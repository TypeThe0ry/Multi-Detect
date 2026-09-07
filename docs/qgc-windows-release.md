# MultiDetectGCS Windows 发布说明

## 当前开发线：v0.3.0-pre.8

`MultiDetectGCS` 从 `v0.2.0` 起使用连续语义版本。当前开发版本是 **`v0.3.0-pre.8`**，最终
**`v0.3.0` 尚未发布**。开发期间只能递增 `pre.N`；只有 REPLAY、SIM、HIL、REAL 的同 UI、
同核心算法、同契约、同安全规则、同 PX4 Mission 路径以及既定硬件/飞行验收门全部通过，才允许
去掉 prerelease 后缀。

### 当前可直接运行的 Windows portable EXE（磁盘实测）

当前推荐入口是
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`。

当前文件为 `46,869,504` bytes，SHA-256
`A1C52D9130386D8FAE4316EE23FD00D68E2141E6A385A7844229CB8680A79412`；portable 目录已经过
install/windeployqt 依赖部署，不能把 build 目录里的裸 EXE 单独复制出去运行。

### v172 回放证据明细、筛选与复制（2026-09-07）

回放弹窗继续使用 native `MultiDetectTargetEventReplayStore` 的已验证事件，新增媒体 kind/encoding/bytes/尺寸/完整
SHA-256、源帧与车辆 AGL/航向明细；“全部/可定位/有效测距/有媒体”筛选和事件级“复制证据”均为只读投影，复制内容
不会触发地图、目标选择、任务、飞控或物理释放。native self-test、Python bundle `--load` 跨语言复核和 offscreen
simple boot 通过；定向回归 `45 passed`，全量回归 `3704 passed, 4 skipped, 0 failed`（3,708 tests）。

v172 改动 UI contract 后，v171 派生证据和 E7 report-only 需按当前 parity 重新绑定；E7 仍为
`gate_passed=false`、`release_allowed=false`、31 blockers，现场网段、Jetson、GR01、相机、室外 GNSS、人工 Fly View、
HIL/REAL、硬件和实飞验收均未声明。

### v171 原生目标事件回放只读查看器（2026-09-07）

Fly View 目标卡片新增“载入回放/查看回放”。选择录制目录中的 `manifest.json` 后，原生
`MultiDetectTargetEventReplayStore` 会在 C++ 侧重算 manifest、`events.jsonl` 与 `media/` 的 schema、路径闭合、
GPS/测距质量、时间线和 SHA-256；任何重复 key、额外文件、篡改或不合格输入都会清空查看器状态。弹窗只读显示
bundle/session、目标、捕获时间、GPS、距离/方位和视频/深度引用，不落地图点、不选目标、不创建任务、不发送飞控指令。

`MultiDetectTargetEventReplayStoreSelfTest.exe` 正向/负向测试通过，并用 Python 生成的真实 bundle 完成 `--load` 跨语言
复核；portable `--simple-boot-test` 退出 0，stdout/stderr 均为 0 bytes，QML 初始化错误数 0。v171 定向回归为
`45 passed, 0 skipped, 0 failed`，全量回归为 `3704 passed, 4 skipped, 0 failed, 0 errors`（3,708 tests）。当前 portable EXE 为 `46,863,360` bytes，SHA-256
`589E33ADAB2C7D1DBC58524BCAE3474D18743A6E0D970FC4C01CF3F187201717`。

以上是 software/native/portable 证据；E7 仍为 `gate_passed=false`、`release_allowed=false`、31 blockers，现场网段、
Jetson、GR01、相机和室外 GNSS 尚未连通，人工 Fly View、HIL/REAL、硬件和实飞验收仍未声明。

### v170 目标卡片实时定位质量状态（2026-09-07）

目标定位卡片与诊断弹窗共用原生 recorder 的 `current_status` 摘要。卡片会显示当前/上次录制状态、定位率、当前
连续不可定位、最近采样断档和告警原因；无录制样本时固定显示 `WAITING`，停止后不会把上次摘要伪装成当前样本。
这条信息仍是只读 advisory，不改变定位坐标 fail-closed、`1σ` 圆、Mission、飞控或物理释放边界。

当前 portable EXE 为 `46,751,744` bytes，SHA-256
`9675AC36032CC990F1F27ACD638F1B4D1D0397F34D9B43810A95D0BCD0D2D263`；offscreen simple boot 退出 0，stderr
879 bytes、SHA-256 `EBB0811E76009EAA70CEC8B19F52B3C2B3232340FAE78CDD7CC67A505644D7F9`，QML 初始化错误数为 0。
定向回归、parity/派生证据和 E7 已重算；E7 仍为 `gate_passed=false`、`release_allowed=false`、31 blockers，
现场网卡/Jetson/GR01/相机和室外 GNSS、HIL/REAL、人工 Fly View、实飞门均未因此通过。v170 定向回归为
`88 passed, 1 skipped, 0 failed`，全量回归为 `3703 passed, 4 skipped, 0 failed`；Ruff、format、compileall 和
`git diff --check` 均通过。

### v169 原生实时定位质量告警（2026-09-07）

当前 portable EXE 已更新为 `46,751,232` bytes，SHA-256
`5BC3761AE4C13C28EBE6BB34691F6EB350CA9EDEF96376DF0DDD03DBE73C5A3A`；路径为
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`，
完整 Qt/GStreamer 依赖已部署。Fly View 诊断弹窗读取 `current_status` 四级实时状态，显示当前连续
不可定位、测距异常、GNSS 不合格计数、最近采样间隔与告警原因；状态只读，不发出飞控指令。

C++ self-test 覆盖 `WARNING → CRITICAL → HEALTHY`，4 样本会话由安装式 session verifier 独立复核；
portable offscreen simple boot 退出 0（stderr 879 bytes，SHA-256
`D2BE8F83902FF9AF48F105685EF17B4571075F826E6A0126603DCBC670D41F81`）。定向回归为
`85 passed, 1 skipped, 0 failed`，全量回归为 `3703 passed, 4 skipped, 0 failed`。E7 仍为
`gate_passed=false`、`release_allowed=false`、31 blockers；现场接口仍未切到 `192.168.144.0/24`，
因此本制品不代表室外 GNSS、硬件、HIL/REAL、人工视觉或实飞验收通过。

### v168 原生实时录制质量时间线（2026-09-07）

Fly View 定位诊断弹窗现在直接读取 C++ recorder 的实时只读质量摘要，显示样本数、定位率、最长不可
定位段、期望/最大/平均间隔、断档、三类质量时间线、分段明细及 epoch 切换。4 样本 native self-test
覆盖连续失败、2,000 ms 断档和 epoch 切换，并由安装式 session verifier 独立复核通过。实时摘要不是
闭合证据，录制停止后的正式结论仍以 manifest 绑定的离线报告为准；功能不发送飞控指令或启用物理释放。
最终全量回归为 `3703 passed, 4 skipped, 0 failed`。

### v167 连续定位质量分段与采样门（2026-09-07）

session verifier 现在输出采样间隔统计/断档事件、连续不可定位段、测距异常段、GNSS 不合格段与 epoch
切换点；`--maximum-non-locatable-run` 和 `--maximum-sampling-gap-ms` 可把现场容许边界变为严格失败门。
真实 C++ 双快照会话以最大间隔 1,000 ms、零异常段通过；全量回归为
`3703 passed, 4 skipped, 0 failed`。本轮仅改审计工具，不重链主 EXE。

### v166 录制 manifest 闭合验证（2026-09-07）

新增闭合 manifest schema、独立安装式 verifier，以及 session CLI 的 `--require-recording-manifest` 强制
绑定。它会重算每份快照的路径、bytes、SHA-256 和诊断语义，并拒绝目录额外文件、重复引用、顺序/时间
错误、符号链接与路径逃逸。v165 C++ 双快照会话已由两层安装式 CLI 复核通过。该轮仅改 Python 审计与
共享 UI contract，主 EXE 继续使用 v165 的 46,718,976 bytes 制品，安全与硬件验收状态不变。最终全量
回归为 `3697 passed, 4 skipped, 0 failed`。

### v165 原生目标定位诊断连续录制（2026-09-07）

Fly View 可开始/停止 `1 Hz` 目标定位诊断录制，单次最多 3,600 个快照。每次录制写入独立目录，正常
停止时原子提交 manifest，并以相对路径、bytes、SHA-256 绑定每个快照；异常退出不会把未收口会话伪装成
完成会话。C++ self-test 发出的 2 份快照已由安装式 session verifier 独立复核通过。

当前 portable EXE 为 `46,718,976` bytes，SHA-256
`8F600A9CE8D1C4E8AF1A63721F16E52A67E56715A81B2EE7D469AED62C1BAB6D`；offscreen simple boot
退出 0。功能仍是 advisory-only，不发送飞控指令或启用物理释放；现场链路、室外 GNSS、HIL/REAL、
人工视觉、硬件与实飞门均没有因此升级。最终全量回归为 `3689 passed, 4 skipped, 0 failed`。

### v164 外场定位诊断会话 verifier（2026-09-07）

安装式命令 `multidetect-target-observation-session-verify` 接收多份诊断文件或目录，验证每个快照后检查
时间/epoch/sequence 连续性，生成逐文件哈希绑定的会话报告。可用 `--minimum-samples` 和
`--minimum-locatable-rate` 设置外场证据门。本轮只增加 Python 审计工具，主 EXE 仍沿用 v162 制品；
工具不会发送 MAVLink 控制或启用载荷。最终全量回归为 `3689 passed, 4 skipped, 0 failed`。

### v163 目标定位诊断离线 verifier（2026-09-07）

本轮增加的是随 Python 包安装的
`multidetect-target-observation-diagnostics-verify` 和仓库脚本
`scripts/verify_target_observation_diagnostics.py`。它们严格验证 QGC 保存的诊断 JSON；加上
`--require-locatable` 后，结构正确但当前不可定位的快照也会以非零退出。原生 self-test 通过 `--emit`
输出实际 C++ 文档，再由两个 Python 入口跨语言复验。

本轮没有修改或重链主 QGC，因此 portable EXE 仍为 v162 的 `46,704,640` bytes，SHA-256
`ECB14FA929AB117EC42178A305D531B8E03FAF1EB283260E551F67E0DF3E28B4`。这项 verifier 是审计工具，
不启用飞控或物理释放，也不替代人工 Fly View、室外 GNSS、HIL/REAL、硬件或实飞验收。最终全量
回归为 `3676 passed, 3 skipped, 0 failed`，JUnit SHA-256
`F9FCCF6AF34849B7BF8D24F3E1F628C1C06771241DAEF2608309C0693629B990`。

### v162 原生目标定位诊断弹窗（2026-09-07）

当前 Release/portable 主 EXE 字节一致：`46,704,640` bytes，SHA-256
`ECB14FA929AB117EC42178A305D531B8E03FAF1EB283260E551F67E0DF3E28B4`。“查看定位诊断”在打开时冻结
当前 JSON，以中文显示 fail-closed 原因、原子身份、目标/源帧、测距和 GNSS 质量与年龄。坐标区域只在
`coordinates_included=true` 时显示经纬度与 1σ；失败时明确省略。弹窗可复制/保存 JSON，但不会发送
飞控指令或启用物理释放。portable offscreen simple boot 退出 0，stdout/stderr 均为 0 bytes。
最终全量回归为 `3660 passed, 2 skipped, 0 failed`，耗时 272.67 秒；JUnit SHA-256
`0D16ED23B6849934FAF176BD6039BFAACAE94B0D11A3066EA1968F1015C41C56`。

### v161 fail-closed 目标定位诊断快照（2026-09-07）

当前 Release/portable 主 EXE 字节一致：`46,702,592` bytes，SHA-256
`729BEDA95DD42E70538D854181113BD09D2414EED4D787AAA11529D1F55C05A0`。目标落图卡片新增“复制定位诊断”
和“保存定位诊断”；JSON schema 为 `multidetect.target-observation-diagnostics.v1`，保存目录仍为
`AppLocalDataLocation/diagnostics`，由 `QSaveFile` 原子提交。

诊断只有在 range/geolocation/GNSS 同原子身份、同目标/源帧、质量合格且源/本地接收均新鲜时才包含
坐标。失败状态也能保存具体原因，但输出会删除 latitude、longitude 和 horizontal sigma。六项原生
self-test 与 portable offscreen simple boot 均退出 0；后者 stdout 0 bytes、stderr 220 bytes，SHA-256
`B7E0728E90A02F3FE1FF8FC9C1CB46B53BA296050981F4F8975A32C27FCA03A3`。这是只读现场诊断，不替代
人工视觉、室外 GNSS、HIL/REAL、硬件或实飞验收。
最终全量回归为 `3660 passed, 2 skipped, 0 failed`，耗时 273.29 秒；JUnit 为 533,660 bytes，
SHA-256 `8443ED203DC647B65EF0D7C4723FD5022BB791B02E746056D021635A756F929C`。

### v160 原生 epoch 诊断弹窗与原子保存（2026-09-07）

当前 Release/portable 主 EXE 字节一致：`46,665,216` bytes，SHA-256
`DE323538B410D6264825B071A083620471A4D726921FD25F10CB859476DB1BAE`。“目标落图”卡片与新的
“生产者 epoch 诊断”弹窗均可复制或保存 `multidetect.producer-epoch-diagnostics.v1`。保存路径位于
`AppLocalDataLocation/diagnostics`，C++ 实现使用同目录临时文件、flush/close 和 rename 原子提交。

五项原生 self-test 通过，其中 depth-grid 使用 Python 生成的 44 个认证 fragment 完成 C++ 重组；portable
offscreen simple boot 退出 0，stdout 0 bytes、stderr 220 bytes（SHA-256
`19AD74FE74B1C4A09CB408506D7523CFF73B9B1134157A8180820CBF8AC4054F`），无 QML 初始化错误。
最终全量回归为 `3660 passed, 2 skipped, 0 failed`，耗时 292.93 秒。
该功能是只读 operator 诊断，固定 advisory-only，不替代人工 Fly View、室外 GNSS、HIL/REAL、硬件或
实飞验收。

### v159 当前 portable 与 producer epoch 诊断导出（2026-09-07）

当前 Release/portable 主 EXE 字节一致：`46,659,072` bytes，SHA-256
`7DA7F882ECB51EEA4DDF1D79FF5F9EBB52E8BD3B77F0DBEEAC1B23EFFB95E375`。producer epoch 初始化、重启、
退休旧包拒绝和切换容量耗尽现在进入最多 64 条的有界事件时间线；总事件数、保留数和丢弃数均可审计。
“目标落图”卡片可一键复制 `multidetect.producer-epoch-diagnostics.v1` JSON，零事件等待状态也可导出。

三项关键 native self-test 退出 0；portable offscreen simple boot 退出 0，stdout 0 bytes、stderr 879 bytes，
且没有 QML 初始化错误；全量回归为 `3660 passed, 2 skipped, 0 failed`。该 JSON 固定为 advisory-only
并明确飞控/物理释放关闭；人工 Fly View、室外
GNSS、HIL/REAL、硬件和实飞门不因此变化。

### v158 producer epoch 健康分级与 evidence v6（历史，2026-09-07）

当前 Release/portable 主 EXE 字节一致：`46,650,880` bytes，SHA-256
`8EBAB7D38113418582AAC12A226BBDFAF988EF30D3E7FCC927222B58E2332A69`。“室外测距质量”面板把 epoch
状态锁存并显示为等待/正常/注意/警告/严重；目标证据 v6 保存健康级别/原因、计数与时间审计字段。

三项关键 native self-test 退出 0；hidden/offscreen simple boot 退出 0，stdout/stderr 均为 0 bytes，没有
QML 初始化错误。这仍是
software/build 证据，不替代人工 Fly View、室外 GNSS、HIL/REAL、硬件或实飞验收。

### v156 当前 portable 与 producer epoch 重同步（2026-09-07）

当前 Release/portable 主 EXE 字节一致：`46,639,616` bytes，SHA-256
`8BDDA7ABB9DF806C5B18DA0CFDD4169CFF1A556DD062B3784F6A09D8074DACF5`。operator protocol type 26
现在为恰好 128-byte application payload，并携带必填非零 `producer_epoch`。QGC 对全新 epoch 先清除
旧 range/geolocation/GNSS、source replay 与 sequence 状态，再原子采纳新三元组；迟到的 retired epoch
永久拒绝。证据新写入为 `multidetect.target-evidence.v5`，v1-v4 只读兼容。

推荐入口仍为
`D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`。
hidden/offscreen simple boot 退出 0，stderr 1,009 bytes、SHA-256
`A51779C621AEDD42B2D38FA63C07B6F4D7A77240784DCFAECF3AD1DBA71F9873`，无 QML 初始化错误。
这是软件构建；人工 Fly View、室外 GNSS、HIL/REAL、硬件和实飞验收状态不变。

### v155 portable 与单包原子目标观测（历史，2026-09-07）

当前 Release/portable 主 EXE 字节一致：`46,633,984` bytes，SHA-256
`788E82B4BF8198EFC8ECD720E2E1A883899D7ADD65DE1276A0221FD61D35AC8F`。它已包含 operator protocol
type 26：range、target geolocation 和 GNSS quality 共用一个认证 envelope，QGC 只会整组三状态采纳；
证据新写入为 `multidetect.target-evidence.v4`，旧 v1/v2/v3 只读兼容。

构建后四项原生 self-test 全部退出 0；portable 使用
`--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output` 退出 0。启动 stderr 为
1,001 bytes，SHA-256 `131508B6705306BD09D6F8889CEDE9B30DCDB37B1526BD742EA2CF1DFB3EC220`，没有 QML
初始化错误。该记录是 software/build 证据，不替代人工 Fly View、室外 GNSS、HIL/REAL、硬件或实飞验收。

### v71 干净 HEAD Combined 复验（2026-09-06）

在 `73429e505d33ca5c645be3d3d0b6cda667f33c5f`、`dirty=false` 的工作树上，使用下述同一 portable
重新完成 900 帧 PX4/Gazebo/QGC Combined：

```text
artifacts/evaluation/combined-sim-qgc-20260905T175019Z-64536/combined-evidence.json
259,353 bytes
SHA-256 5031EEFE75CF78D42AECCC04E990BB8F9975FBC6EBB12D4087F685C18652C8B6
```

900/900 sensor、RTSP、type-23/type-24 和 full odometry 完整；832 份 range/geolocation 中 336 份
合格坐标、496 份 fail-closed withheld，0 operator transport error。exact PID/HWND 上第一次点击
`flame` 候选框，相关 `SELECT_TRK` ACK、TRACKING 和 QML `TRK` adoption 一致；VTOL Mission 完成
MC/FW/MC、land/disarm，最大离家 `226.949 m`、最大相对高度 `26.078 m`。独立 verifier 44/44，
报告 SHA-256 为 `0C27451F88A0FD3655740523525D97BAE98D8D981EE0A3CC60DB8178B9A1136D`。
这仍是 software/SIM machine proof，人工视觉和 HIL/REAL/硬件/实飞门未提升。

### v70 当前 portable 与原生目标点击闭环（2026-09-06）

当前 portable 主 EXE 为 `46,579,712` bytes，SHA-256
`FFCC4FCF45EAE6E7A52ED6B2585BCCF29E5F36776D03D2EFA7F6527D1A96255B`。必须从完整 portable
目录启动，不能只复制裸 EXE。

`combined-sim-qgc-20260905T172856Z-51520` 在 exact QGC PID/HWND 上按 QML 候选框中心真实点击，
第一次命中后发送 `SELECT_TRK`，ACK、TRACKING 和 QML `TRK` adoption 均绑定同一目标；随后 900 帧
runtime/type24 全闭合，831 份 range 与 831 份 geolocation 状态进入 QGC transport，0 transport error。
本记录证明机器交互与 transport，不声明人工渲染签字；`operator_visual_acceptance=not_claimed`。
必须从完整 portable 目录启动，以便同时加载 Qt、QML、GStreamer、翻译和插件运行时；裸
`Release\MultiDetectGCS.exe` 只是链接产物，不是单文件发布物。当前 Release 与 portable 主 EXE
在本机字节一致：`46,579,712` bytes，SHA-256
`FFCC4FCF45EAE6E7A52ED6B2585BCCF29E5F36776D03D2EFA7F6527D1A96255B`。v70 已为原生目标点击和
exact-PID/HWND 捕获重新链接 QGC；operator protocol 原生 self-test 退出 `0`。旧版本/旧构建
的大小和 SHA 记录保留在下文历史条目中，不覆盖本段当前指纹。

### v69 combined 独立传感器硬门

当前权威 source：

```text
artifacts/evaluation/combined-sim-qgc-20260905T152520Z-75420/combined-evidence.json
git_head=46920e1c80041664ba53813ed3841a5887bd5f20; dirty=false
gate_passed=true; sensor/RTSP/type23/type24/odometry=900/900
independent_verifier=44/44
```

source 为 255,256 bytes / SHA-256
`9CAB1BC405571C2F287D1545D77B7EA1F7C4A56CC163AB76E1402CB362DB121B`；独立报告为 2,139 bytes /
SHA-256 `1EEFD154DF164F307D62EF1CFFCC7479E343B8E3FC6A7017A9BBE61EB19502D0`。新增检查会拒绝
CameraInfo pending/missing、sensor count 漂移和逐帧 odometry 缺失。portable EXE 未重链接；人工视觉、
HIL/REAL、硬件和室外门保持未通过。

### v68 CameraInfo 语义与 combined SIM/QGC/Mission 复验

当前权威运行：

```text
artifacts/evaluation/combined-sim-qgc-20260905T150510Z-27780/combined-evidence.json
git_head=c0a4c3256b9d4db45f01174614a672d87c281281; dirty=false
gate_passed=true; sensor/RTSP/type23/type24/odometry=900/900
camera_info=validated_k_p_rectification_plumb_bob_distortion
```

source 为 256,194 bytes，SHA-256
`E800968AB63BF847CA53BE7798EAD3B2501B731E958D76AB5E8EAC447A999471`；独立 verifier 41/41，
SHA-256 `3D58600C9B57ED981C651DE79CCA90F0FED275096E9FD5CA416B104016E51078`。CameraInfo 的 K/P/R、
PLUMB_BOB 与 D 已与仓库标定绑定，状态进入 runtime/frame-set。7-item Mission 完成 readback hash、
START_PERMITTED、AUTO.MISSION、VTOL MC/FW/MC、land/disarm；容器和全部测试端口完成清理。

PiP 仍使用当前原生 QGC surface。为消除 Windows 前台竞争，pointer placement 最多执行 8 次有界重试，
但每次都必须同时满足 exact coordinate、QGC PID/HWND ownership 与 foreground；本轮第一次即通过。
portable EXE 没有发生 C++/QML 改动，继续使用本页上方同一指纹。本轮仍未产生四模式人工签字，
HIL/REAL、真实硬件、室外 GNSS/RTK 和实飞不随 SIM 结果升级。

### v67 当前源码 900 帧 combined SIM/QGC/Mission gate

当前成功运行：

```text
artifacts/evaluation/combined-sim-qgc-20260905T142324Z-32372/combined-evidence.json
git_head=005da90c3aba2d079ed2ef3aa2e7fc147202d4c4; dirty=false
gate_passed=true; frames=900; type23=900; post-adoption type24=900
```

source 为 255,480 bytes，SHA-256
`FD386165212A68C6E2D053A0D58B05D796565AB716CB088439F0A6756EDC1090`。独立
`verify_combined_sim_evidence.py` 输出 2,006 bytes、SHA-256
`C195FFC92E88F53C54C4AC07C62891BD3806C13E97B47692F895DB9988DEAD60`，41/41 checks 通过。
传感器 helper 收到 1,298 个 image callback，其中 4 张因 NavSat/Odometry 短暂落后超过 1.5 秒被安全
丢弃；最终 900 帧全部带因果、新鲜的动态遥测，helper exit 0。容器已移除，9 个动态 loopback 端口
全部释放，受保护 14550 前后为空。

通用四模式 machine-binding adapter 对此 source 的 SIM 校验 19/19 通过；同一 source 改以 HIL 模式
绑定时 `runtime_mode_matches=false`，因此不能用 SIM 证据填充 HIL 人工记录。本次只读预检未生成
operator accepted record。

该运行复用了上方现有 portable EXE，未修改 QGC C++/QML，因此没有重复链接。它只证明 software-only
SIM 机器链；desktop GPU ROI 在 GDI 截图中仍不作证明，operator visual 仍是 `not_claimed`，目标定位门
在此场景中未配置，CameraInfo K/P/畸变语义、HIL/REAL、硬件和室外 GNSS 均未关闭。

### v66 四模式 native capture v2

`scripts/capture_qgc_native_style.ps1` 现在要求调用方显式传入
`-RuntimeMode replay|sim|hil|real`。输出 `process-observation.json` 为
`multidetect.qgc-native-style-capture` schema v2，并由
`configs/schemas/qgc-native-style-capture.schema.json` 校验；它包含 event、run ID、EXE identity、canonical
release-parity identity/五组摘要、exact PID/client rect、遮挡和 PNG identity；`capture_gate_passed`
只代表机器捕获链通过，不代表人工视觉签字。HIL acceptance wrapper 已自动传入 `hil` 和同一 run ID。

人工记录生成器优先读取这一通用格式；历史 `px4_sitl_qgc_operator_acceptance` 仍可作为 legacy HIL
来源，但其 event 在 REPLAY/SIM/REAL 下必然 fail-closed。四模式各自完成机器捕获后，仍须分别用同一
12 项 checklist 生成 accepted review，不能复用截图或 machine report。

### v55 PX4 position-response acceptance；portable 不重复链接（2026-09-05，pre.8）

v55 只修改 Python 的 disposable PX4/Gazebo AI acceptance、负向回归与文档；没有改变 QGC C++、QML、
资源、operator protocol 或 CMake。因此没有重新链接一个内容相同的新 EXE，canonical portable 仍是
46,576,128 bytes / SHA-256
`06E7B1BBEC1B96DC6E5A231B12B88725122476075ED86A24BBEF554CB899E5FE`，必须保留完整 portable
目录运行。

`px4_sim_vehicle_motion=passed` 在 v55 中仅表示：truth-free synthetic target 经共享 AI/authority/
watchdog 和 loopback MAVLink `MANUAL_CONTROL` 后，单个 x500 disposable SITL 的
`GLOBAL_POSITION_INT` 给出超过门限的 WGS-84 position response，且持续刷新、armed POSCTL、heartbeat
连续性和零 rejection/failsafe 条件同时通过。它不是 QGC 启动、渲染或人工 Fly View 验收，也没有证明
camera/ranging/geolocation/evidence-card 链、actuator telemetry、fixed-wing/VTOL、硬件、室外 GNSS、
HIL 或 REAL；`operator_visual_acceptance` 继续保持 `not_claimed`。

v55 current-code 全量 Python 回归为 2,907 tests：`2906 passed, 1 skipped, 0 failed, 0 errors`；JUnit
411,702 bytes，SHA-256 `A2C2595C49D0B663468D09274D22976CC1B4D9128425BB23458445B1C5397F51`。该测试结果不会替代
QGC portable 构建、自检、offscreen boot 或人工视觉门。

### v52 Python 共享地形测距；portable 不重复链接（2026-09-05，pre.8）

v52 增加的是共享 Python DEM/GNSS 测距、telemetry/replay 和 schema，不涉及 QGC C++、QML、资源或 CMake。
因此没有用一次无意义的重复链接改变已验证二进制身份；当前 canonical portable 仍是上面的
46,576,128-byte / `06E7...E5FE` 文件。QGC 继续只消费既有认证 `RANGE_STATUS` 与
`TARGET_GEOLOCATION`，UI 风格、Fly View 布局和协议字段未分叉。

v52 三机型 binding 只做后台 deterministic protocol/asset preflight，明确记录人工视觉和硬件
`not_claimed`。它不是 GUI 打开验收，也不改变最终发布门。

### v51 RANGE_STATUS bit 31 与 portable 重建（2026-09-05，pre.8）

QGC C++ reason registry 新增 `target_geometry_uncertainty_unbounded`，reason mask 扩展为完整
32 bits，并与 Python wire 列表保持顺序一致。增量 Release 构建、portable 部署和 operator protocol
self-test 成功；Release 与 portable 主 EXE 字节一致。用户运行时仍应启动上面的 canonical portable
入口，不能只复制裸 EXE，因为 Qt/QML/GStreamer/plugin 依赖仍在相邻目录。

本轮没有进行前台人工 Fly View 检查，`operator_visual_acceptance` 仍是 `not_claimed`；真实硬件和
HIL/REAL 门也未因构建成功而改变。

### v49 模式 3 输出锁定横幅与 portable 复验（2026-09-05，pre.8）

模式 3 的“正在瞄准”现在要求当前飞控输出权限为 true；已 LCK 但 provider 锁定时显示
“模式 3 目标已确认 · 飞控输出锁定”，并明确“仅显示姿态建议；不会发送姿态或模式命令”。实现继续使用
QGC 原生控件与布局，没有新增 simulator-only view。

构建命令仍为：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\build_multidetect_windows.ps1 `
  -Configuration Release -SkipConfigure -BuildSelfTests -InstallPortable -Parallel 4
```

构建、三项原生协议自测、44 分片 Python→C++ depth-grid 重组与隐藏 offscreen simple boot 全部通过。
自测证据在
`artifacts/evaluation/terminal-continuation-v49/qgc-native-selftests-v49.json`，汇总验收在
`artifacts/evaluation/terminal-continuation-v49/qgc-mode3-output-lock-acceptance-v49.json`。全回归为
`2826 passed, 1 skipped`（2,827 tests）；人工 Fly View 视觉验收没有被 offscreen 测试替代，仍为
`not_claimed`。

### v35 加速度预算完整性门与当前 E7 绑定（2026-09-04，pre.8）

v35 当时，目标帧与 GPS 时间对齐时，若 telemetry 给出 `acceleration_uncertainty_mps2` 但缺少完整 N/E
速度，目标投影 fail-closed，避免静默丢掉运动误差预算。当前 parity SHA-256 为
`D9CF6D9E625E6B0ADB56084628CCB39ED358E2156AF89201633C2F6E1F2B9AEF`，core hash 为
`7D35526D63E3BFE247776F1ED2CC14F2950E0E61AB72E10A170C5C41BB930CF3`。wrapper 默认使用 v35
external join/revalidation（SHA-256
`B459F6CDA7E6BD60EE5BED486E56F25870D388E31650F54333AC2FA3741CD12B` /
`D825A2A8A1D8E3F152C35C6C8C70C3D847D9C26E3329A4F8A7EE2D27974B338C`）；E7 manifest/report
SHA-256 为 `B4801000CCF23B3243ADCBAA4788C00539973A0BA1A3D6D48233758782A728E4` /
`2E4CCE1FA23899958BBF05E5F399171CA462B83745D6435509BF5AEEC2FE71E7`。总门继续关闭，版本仍为
`v0.3.0-pre.8`。

### v34 运动补偿不确定度与 replay-set 重绑（2026-09-04，pre.8）

> v34 是上一份 parity snapshot；其 external/E7 文件保留为历史证据，当前 wrapper 不再选用。

Python 核心新增可选 `acceleration_uncertainty_mps2`：目标投影均值保持恒速，水平运动误差按
`hypot(|dt|·σv, 0.5·dt²·σa)` 进入 `1σ` 落图圆；非法值或缺少完整 N/E 速度 fail-closed，旧 telemetry
缺省保持兼容。
这次只改 Python/证据契约，不改变 QGC C++/QML，因此无需重新链接当前已验证的 portable EXE。

由于 telemetry identity schema 扩展，历史 Gazebo replay-set 的 recording hash 按新代码重算后会
发生变化。老目录 `artifacts/replay-sets/sim-gazebo-2026/` 原样保留；v34 当时的 wrapper 改用从同一
capture 重新生成的 v34 副本
`artifacts/evaluation/terminal-continuation-v34/replay-set-sim-gazebo-2026/`。该副本 manifest
为 4,965 bytes、SHA-256 `F577BF8085F33311D20AE27BB882C5C5F0F7225FC6F86B379A5384982E1FEDFF`，
canonical hash `F2834DC4CD03C95DC6502C137EAA66430A3F68CC2F145498889156621FA32E4C2`；六层仍是
`synthetic_demo` 空基线，accuracy/release gate 继续关闭。

v34 external join/revalidation 当时通过独立重算，SHA-256 分别为
`80599422FD5D39E3445F8D3ADA5473CAC0F62D184023A2B2F2BD4CFF6E46C1FE` /
`06D4C67C5A095661F5D54A691C334C8F6D2576E5821CB365166C8CE2E28E1BAE`。v34 阶段的 report-only E7
快照在 `artifacts/evaluation/terminal-continuation-v34/release-gate-current/`，manifest/report
SHA-256 为 `7C08D3254C49DB9531D1B1032939FF1D4C01426164299C0EEF212A0B6C661FBF` /
`12C8C52A9F58EEA97EC6AF863295BAAA73605CC65D1AC0B83408BD48D75156C5`；总门仍
`gate_passed=false` / `release_allowed=false`，版本保持 `v0.3.0-pre.8`。

### v32 导航速度残差不确定度膨胀与 E7 重新绑定（2026-09-04，pre.8）

`NavigationVelocityFusionEngine` 在 GPS/VIO/空速源通过 `3.5σ` 成对一致性门后，继续计算
North/East 两轴残差 `χ² / (2*(N-1))`。Birge ratio 会在源分歧超过声明协方差时放大融合
速度 `σ`，追加 `navigation_velocity_uncertainty_inflated` 并保持 `DEGRADED`、
`advisory_only=true`；GPS `fix_type>=3`、卫星 `>=6`，`255` 未知哨兵不被当作有效数量。
该 Python 核心路径不产生飞控输出，所以 portable QGC 无需重新链接。

验收记录
`artifacts/evaluation/terminal-continuation-v32/navigation-residual-inflation-acceptance-20260904.json`
为 3,351 bytes、SHA-256
`AA1E0EF13A40F9EDBD777B5D214F1601299E280DFF8E4B2B40353FF5037D47C7`；nominal/spread
场景与 18 个专项测试通过。当前 `configs/release-parity.json` SHA-256 为
`723CC760FE277DB378BB3FA55BDB0D54149F72AF38C29D2EEC67EC95BA434B4B`，core hash 为
`354d6a61f1dc986eca15e30fefff7ee820e7d83d0c3a18f4071904fc2a194d35`。

加入验收记录自身的绑定测试后，严格全量回归为 `2670 passed, 1 skipped`，退出码 `0`；日志
`artifacts/evaluation/terminal-continuation-v32/full-regression-v32-navigation-residual.log`
（3,078 bytes，SHA-256
`F1B63141BC7B6B05B897711C30EF97BB2230F779633CA71833537B62EE360656`）。

基于新 parity 重新运行的三机型六源 external SIM/runtime + AI 证据：VTOL 新鲜源
`simulator-external-runtime-ai-evidence-20260904-current.json`（118,148 bytes，SHA-256
`081B922CD725ACD1F42ED218F142E7AE1E8E5A684CA02F622B4AECE738E12DA2`，`ai_steps=2`、
armed heartbeat、truth-free `TRACK` 通过）；join
`simulator-external-runtime-ai-join-20260904-current-pre8.json`（5,491 bytes，SHA-256
`7AA749EB13AA18A42A734D890401588B9885A7805969CEE2BAF3AB500923E392`，通过）；独立
revalidation
`simulator-external-runtime-ai-revalidation-20260904-current-pre8.json`（561 bytes，SHA-256
`F5D1F057D85498C8E1A8BC563D950419E2D5AAAA9FDBBB28D13DF768D8A7348F`，
`source_recomputed=true`、通过）。

当前 wrapper report-only E7 快照位于
`artifacts/evaluation/terminal-continuation-v32/release-gate-current-20260904T070708Z/`：
manifest 20,885 bytes、SHA-256
`064FD23D88A0FA65280D4493A6AB9330748DC8D23C1FA261557DC8E7E515EBA4`；report 54,111 bytes、
SHA-256 `87D0D7F61FC802DBA66D54A592D3ED0AB30EB83115ADEBB091162D1E580D9187`。external、
scene-stream set 与 parity 子门通过，但总门仍 `gate_passed=false` / `release_allowed=false`、
31 条阻断；现场 Jetson/GR01/相机 route、真实 GNSS/Pixhawk、HIL/REAL、人工 Fly View、
replay-set 和最终 `v0.3.0` release channel 仍待现场验收。Docker 记录是 software-only 证据。

构建脚本以 `-SkipConfigure -BuildSelfTests -InstallPortable -Parallel 4` 复核成功（退出码 `0`，
`ninja: no work to do`）；Release/portable EXE 字节一致，46,575,616 bytes、SHA-256
`4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`。构建后
`--offline --simple-boot-test` 退出码 `0`，日志
`artifacts/evaluation/terminal-continuation-v32/qgc-simple-boot-v32.log`（350 bytes，SHA-256
`FA85CF0BA419D1335A2695DF9C9E44FAB4F2BA19C4EB1CBFA08DA60D3CF762F7`）。

### v33 现场链路只读预检刷新与 E7 重新绑定（2026-09-04，pre.8）

台式机现场预检重复 2 次执行 Ethernet/协议/被动 route 检查，报告/接口诊断结构校验通过，
但 `Ethernet=192.168.1.13/24` 不在 `192.168.144.0/24`，Jetson `:22`、GR01 `:5760`、相机
`:554` 均超时，`hardware_link_ready=false`。报告
`artifacts/evaluation/terminal-continuation-v33/field-link-preflight-live-20260904T-current.json`
为 12,923 bytes、SHA-256 `2B3694E74332B96B3257F405842B3584A44AE07D5FCEA8EC5E47B07DF31E651A`；
接口诊断为 4,412 bytes、SHA-256
`9800D930FD0FAF1D945420BF3849175F165DC463AEBD3308411143BDD99E9D68`。本次只读，不改网卡/IP/
路由，不登录 Jetson，不请求 MAVLink stream，不启动 RTSP，不发送控制数据。

用该报告重新绑定的 report-only E7 快照在
`artifacts/evaluation/terminal-continuation-v33/release-gate-current/`，manifest/report SHA-256
分别为 `C66DB565A12DC664C28F4DB12E38AD7E6611E28A14F650839362A18EDB817C19` /
`340CE3C01968194B77010A808AE8A09BD62F269D9D56C786224E88099A3827D1`；总门仍
`gate_passed=false`、`release_allowed=false`，31 条阻断保留，版本仍 `v0.3.0-pre.8`。无参数
wrapper 默认现指向 v33 现场报告；接通飞机网段并确认机载 IP 后再重跑预检。

### v31 多源测距残差方差膨胀与 E7 重新绑定（2026-09-04，pre.8）

> 历史条目：当前默认输入已切换到上方 v32 导航残差快照；本节 hash、join 与 E7 路径仅供
> provenance 对照。

当前 Windows portable 所使用的 Python 核心已加入多源 direct-range 的加权残差
`χ²/(N-1)` 检查。100/100 m 对照保持历史区间，100/110 m 的一致性门内 spread case 会
按 Birge ratio 放大 `σ`，写入 `direct_range_uncertainty_inflated` 并把更宽的 CI 传给目标
落图；两种结果都保持 advisory-only。可重算记录
`artifacts/evaluation/terminal-continuation-v31/ranging-residual-inflation-acceptance-20260904.json`
的 SHA-256 为 `88516D2203D168C894945EDC5F1D7DFA6569F07B8DF9DD8ED910058BC27FA2DA`。

本轮 source parity 更新后，Docker Desktop Server `28.4.0` 已完成 VTOL live-AI 外部运行，
并将三机型六份独立 runtime/AI 源记录重新绑定到当前 parity。join
`artifacts/evaluation/terminal-continuation-v31/simulator-external-runtime-ai-join-20260904-current-pre8.json`
为 5,491 bytes、SHA-256 `0681A9BC24883DBE284793217E9DBC79C62B40A920B2372625714A53010EE1DD`，
独立 revalidation 为 561 bytes、SHA-256
`DDF73895D5D8E6B4E7B51670E7644972248F41B17286E32AA84507369D6C9C6E`，两者均通过且
`source_recomputed=true`。当前 report-only E7 绑定位于
`artifacts/evaluation/terminal-continuation-v31/release-gate-current-20260904T065500Z/`：manifest
为 20,856 bytes、SHA-256 `CA97AE02767F93B691A3CB2B41F823FA0E53EB1D5D6B726C657D086861F523E1`，
report 为 54,109 bytes、SHA-256
`2E66751A6D7E0DEB16276431C5F9DECE74969D783C3EC78461EDD95FE0EC9B6E`。新清单绑定 core hash
`4ed386e9eaf91952b7c7be56d3d272efeaca4bb70a50e78ed57e04aa5307f5ad`，external SIM 与
scene-stream set 子门均通过；现场 preflight freshness 通过但 Jetson/GR01/相机和
`192.168.144.0/24` route 仍不可达，`hardware_link_ready=false`。当前总门为
`gate_passed=false` / `release_allowed=false`，31 条阻断原因仍在；已有 portable EXE 的
启动结果不受影响，但现场 GNSS/Pixhawk/Jetson/相机、HIL/REAL、人工 Fly View 和最终
`v0.3.0` 仍待验收。external 证据只证明 software-only PX4/Gazebo/AI 进程链路。
本轮严格全量回归为 `2668 passed, 1 skipped`、退出码 `0`；日志
`artifacts/evaluation/terminal-continuation-v31/full-regression-v31-external-rebind-20260904.log`
（3,078 bytes，SHA-256 `837E9E87D8B5F40EB93001BDB8F674CF151B97D0E4DEFD0DD291934FD15A86F8`）。

### v30d scene-stream set E7 绑定与现场链路续检（2026-09-04，pre.8）

三机型 `scene_stream_set` 已进入正式 E7 manifest binding；当前 bound manifest/report 为
`artifacts/evaluation/terminal-continuation-v30/release-gate-continuation-20260904T133400Z/release-gate-pre.8-current-20260904T133400Z-v6-bound.json`
（20,828 bytes，SHA-256
`98A301DA0147010CBF6F927122304E3AE1F47B08B095B8E9D89D535A2C37547C`）和同目录的
`artifacts/evaluation/terminal-continuation-v30/release-gate-continuation-20260904T133400Z/release-gate-pre.8-current-20260904T133400Z-v6-bound-report.json`
（52,806 bytes，SHA-256
`1A8B3A8402132B02788217787D564B32F87E2552506152232843B8D528C61EE8`）。报告确认
`simulator_scene_stream_set.present=true/passed=true`、fixed-wing/multirotor/VTOL 3 条记录、
9 项 checks 全通过；总 `gate_passed=false` / `release_allowed=false`，仍保留 31 条现场、
replay-set、人工视觉、Mission、HIL/REAL 和 prerelease channel 阻断原因。

本轮绑定使用的新鲜只读现场预检为
`artifacts/evaluation/terminal-continuation-v30/field-link-preflight-continuation-20260904-133250.json`
（17,366 bytes，SHA-256
`F5E12FB3A202F87B0102FBBB0DEE122538D1328FEB292CCDF4FE68E4ADAF4B1F`），请求/收集样本数为
3/3，但 Jetson、GR01 与相机端点仍超时，route 源地址 `192.168.1.13` 不在预期
`192.168.144.0/24`，所以 `hardware_link_ready=false`。结构化摘要
`artifacts/evaluation/terminal-continuation-v30/scene-stream-set-release-gate-acceptance-20260904-v3.json`
（2,858 bytes，SHA-256
`3FAAEF71A53E303B56EFBA550EDCD57A41C704894F860FA6DD3D080EF7D81214`）保留
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending`；本轮 85 项专项
与全量 `2667 passed, 1 skipped` 均通过，但不升级室外 GNSS/Pixhawk/Jetson/相机、HIL/REAL、
人工 Fly View 或最终 `v0.3.0`。

### v25 现场链路 freshness-bound wrapper 复核（2026-09-04，pre.8）

当前 wrapper 已用新现场 watch 报告重新写入
`artifacts/evaluation/terminal-continuation-v25/e7-freshness-bound-900/`。输入报告
`artifacts/evaluation/terminal-continuation-v25/field-link-preflight-live-20260904a.json`
的 SHA-256 为
`53E45C0C4D7B6EB7942BB7438A90F29BC422893E3E6259F97A8A4C33EF74AB39`；manifest SHA-256
为 `6C854C43E932FF4CC010B099A44ACED2CF62749C3A86D8405F27C6CFB6271CA4`，report SHA-256 为
`E192B4E7BED44B2B13660F58A019CF2CCAE572DB12D8C2F2012E4FCC67AD6B84`。`max_age_s=900` 与
`report_timestamp_epoch_s` 成对存在，schema 校验为 `0` 个错误，绑定时年龄约 `157.1 s`；
设备端点仍不可达，故 `hardware_link_ready=false`、`gate_passed=false`，不代表室外 GNSS、
真实 Pixhawk/Jetson、HIL/REAL 或最终 `v0.3.0` 已通过。当前工作树全量 Python 回归收集
`2625` 项，结果 `2624 passed + 1 skipped`（Windows symlink 权限），日志
`artifacts/evaluation/terminal-continuation-v25/full-regression-final.log` 为 3,492 bytes，
SHA-256 `74CCDE8D3C72276B8122DFB03579D1E76CAF98AAD0F5030F6F7AEB7DD3CDC0B7`；ruff 与
compileall 均返回 `0`。同一 v25b 报告绑定到完整 goal-acceptance 后，快照
`artifacts/evaluation/terminal-continuation-v25/goal-acceptance/multidetect-goal-acceptance.json`
SHA-256 为 `4514AF0836C0E33117475B3BD1C272F24D94A857B215C253BAFE05CFCE16F5D7`，日志
SHA-256 为 `78342FAA5DDC8B43BBDDDC1EAC986A1F494F693E87AFED36366479CE03A56F59`；核心
`2625`、QGC custom `66` 和 SIM/external 子门通过，但 `hardware_status=offline_pending`，
因此 portable EXE 的软件验证不等于 HIL/REAL 或最终 `v0.3.0`。

### 2026-09-04 goal-acceptance v24 freshness-bound 快照（pre.8）

最新完整快照为
`artifacts/evaluation/terminal-continuation-v24/multidetect-goal-acceptance.json`，46,146
bytes，SHA-256
`5E5820365FD95E8BBC3987E8559A88878922CC3E3CD478D3A60AB6272C53E3C3`；日志为 58,167 bytes，
SHA-256 `CB4541FEAF9030022B4ADDAB7D7BEF9F06E4A9073E9D671D5399EB0860B33BF7`。快照确认
`software_status=pass`、核心收集 `2624`、QGC custom `66` 和三机型确定性 SIM 子门通过；
现场报告 freshness 在 `900 s` 窗口内，但 `hardware_status=offline_pending`、
`field_link_ready=false`，六项 live gates 继续阻断。

v24 writer/evaluator 已把 REAL `field_link` 的 `max_age_s` 和
`report_timestamp_epoch_s` 成对写入并重验；watch 报告
`artifacts/evaluation/terminal-continuation-v24/field-link-preflight-live-20260904e.json`
的 SHA-256 为 `2341C97D1146CC33D0734D7947A5A543D05A6D96C7E6C8B0896E1C974EA6B5F2`。
这只收口证据新鲜度，不代表真实 Jetson/Pixhawk/相机、室外 GNSS、人工 Fly View、HIL/REAL
或最终 `v0.3.0` 已通过。

### 2026-09-04 goal-acceptance v23 最新 freshness 快照（pre.8）

freshness 缺失时间戳回归后，当前台式机重新执行的完整验收快照为
`artifacts/evaluation/terminal-continuation-v23/multidetect-goal-acceptance.json`，46,003
bytes，SHA-256
`ABB54E58F04C12330E37B3F2574DF73979B8C6224DC8D8F5EDDEF9BDE2D35DEB`；日志 SHA-256
`D26E844E778D53AB926077935E8980464C565109CCA9445DF4859B0D90F68762`。快照确认
`software_status=pass`、核心收集 `2623`、QGC custom `66`、三机型确定性 SIM 子门通过；
freshness 在 900 秒窗口内，但现场仍为 `hardware_status=offline_pending`、
`field_link_ready=false`，不代表 HIL/REAL 或最终 `v0.3.0` 通过。

### 2026-09-04 field-link freshness 绑定与 goal-acceptance v22（pre.8）

现场报告绑定现在带有 900 秒默认 freshness 窗口：one-shot 使用
`generated_at_epoch_s`，watch 使用 `finished_at_epoch_s`；缺失、未来或过期报告会保持
fail-closed。v22 现场报告
`artifacts/evaluation/terminal-continuation-v22/field-link-preflight-live-20260904c.json`
的 SHA-256 为 `0F211E23FD0C2671C8424C276E41993EB7DABB15059A4585A044B000D3F1D682`，年龄约
20.5 秒时 freshness 通过，但 `hardware_link_ready=false`。
对应总验收快照
`artifacts/evaluation/terminal-continuation-v22/multidetect-goal-acceptance.json` 为
46,003 bytes，SHA-256
`B2569EAF162AD9E33266DE25ABC1D61EEFB7A214C9021807A02A80C7078CD8B2`；它确认
`software_status=pass`、核心收集 `2622`、QGC custom `66`，不代表现场、HIL/REAL 或最终
`v0.3.0` 通过。

### 2026-09-04 goal-acceptance v21 当前快照（pre.8）

当前台式机重新执行的完整验收快照为
`artifacts/evaluation/terminal-continuation-v21/multidetect-goal-acceptance.json`，45,234
bytes，SHA-256 `78498FAA088110E49B640BF3BA605B23B133142BC10AAFC6B184E1091F53D089`；运行日志
为 `artifacts/evaluation/terminal-continuation-v21/goal-acceptance-rerun.log`，57,303 bytes，
SHA-256 `F23336F60FE289A63DDAE65E9551180A5B594B28EE4924FD2181352D1913292F`。快照确认
`software_status=pass`、核心收集 `2619`、QGC custom `66`、当前三机型确定性 3-D SIM
证据 `gate_passed=true`；现场仍为 `hardware_status=offline_pending`、
`field_link_ready=false`，六项 live gates 保持阻断。它不是外部 PX4/Gazebo、室外 GNSS、
人工 Fly View、HIL/REAL 或最终 `v0.3.0` 发布证据。

### 2026-09-04 三机型 SIM 绑定证据（pre.8）

VTOL、固定翼和多旋翼的确定性 3-D 控制预检已在当前台式机重新生成，并绑定到
`artifacts/evaluation/terminal-continuation-v20/simulator-3d-control-binding.json`。
文件大小 2,284 bytes，SHA-256
`967C162D4B996C4CA709B678732A8690FFA18256B6AD958CDCC08EE6CAC61B4E`；绑定验证的
`all_airframes`、`all_deterministic_gate_passed`、`records_hash_bound` 均为 `true`。
这只是共享契约和仓库资产的确定性证据，外部 PX4/Gazebo、GPU/人工 Fly View、HIL、REAL
仍未宣称通过。

### 2026-09-04 goal-acceptance v19 绑定结果（pre.8）

当前台式机重新执行的完整验收快照为
`artifacts/evaluation/terminal-continuation-v19/multidetect-goal-acceptance.json`，45,234
bytes，SHA-256
`5C6E0618F0C25A04A52832F7B00CB5113EF21BC3C2C1375F658899A8B2A04066`；运行日志为
`artifacts/evaluation/terminal-continuation-v19/goal-acceptance-rerun.log`，57,255 bytes，
SHA-256 `CF2EC57CE361AD811733986F96BA7606005EF4BE347E80BE0F78E444FA4EC392`。该快照确认
`software_status=pass`、核心收集 `2619`、QGC custom `66`，同时保留
`hardware_status=offline_pending`、`field_link_ready=false` 和六项现场/REPLAY live gates；
它不是室外 GNSS、真实硬件、HIL/REAL 或最终 `v0.3.0` 发布证据。

### 2026-09-04 Windows build wrapper 哈希收口（pre.8）

`scripts/build_multidetect_windows.ps1` 的最终摘要改用 .NET `SHA256::Create()` 流式计算，
不再依赖可选 PowerShell 哈希模块。Windows PowerShell 5.1 执行
`-SkipConfigure -BuildSelfTests -InstallPortable -Parallel 4` 返回退出码 `0`，日志
`artifacts/evaluation/terminal-continuation-v18/build-wrapper-after-sha256-fix.log` 为 142,536
bytes、SHA-256 `F28147F8FEBEF709936A31BEFD88EA7B6243AB294A00C1CF447E9AFBD2752E23`。同一命令在
pwsh 宿主下也返回 `0`，对应日志
`artifacts/evaluation/terminal-continuation-v18/build-wrapper-pwsh-after-sha256-fix.log` 的大小和
SHA-256 与上面完全一致。Release 与
canonical portable `MultiDetectGCS.exe` 均为 46,575,616 bytes、SHA-256
`4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40` 且字节一致；portable
`MultiDetectReplayRtspPublisher.exe` 为 95,744 bytes、SHA-256
`DE94D5404BEF48B80919BFC06465BD53EBFD50A8B65A50F38DA3BCDDADB58312`。构建后的 portable
`--offline --simple-boot-test` 退出码为 `0`，日志
`artifacts/evaluation/terminal-continuation-v18/qgc-simple-boot-after-sha256-fix.log` 为 220
bytes、SHA-256 `946D08771ACF7DE07C43218D395CC514F16CDB253A34C9AC5D4601F70299BEA9`。这只修复
构建 wrapper 的跨宿主稳定性，不改变现场、室外 GNSS、HIL/REAL 或最终 `v0.3.0` 发布条件。

2026-09-04 的导航 GPS 质量收口已经进入当前源码：速度融合默认复用 canonical `fix_type >= 3`
和已知卫星数 `>= 6`，配置不能放宽这两个下限；速度观测另有 `age <= 0.75 s` 的严格窗口。
相机标定文档、室外深度标定文档和 JSON 样本同样采用拒绝重复 key/非有限数字的严格解析。
当前 parity SHA-256 为 `BFE773CE40773A9AFA651E29FC2BA80FB1119B6B2519424A7A2A2348302BED24`，
`core_algorithm_hash=994ae1dffb3856eac5d71b479a7377f3f1b1c5beda2d154d6c2ee3b733c15af3`。
系统 Python fallback 的最新全量回归收集 `2617` 项（`2616 passed / 1 skipped`），日志
`artifacts/evaluation/terminal-continuation-v18/pytest-full-20260904-after-goal-exitcode.log`
的 SHA-256 为 `E688279FEF98088AD9874A63B65D4477E3AC176B8808CFEC95B6E155FDB357FA`；canonical
QGC venv 收集 `2619` 项、退出码 `0`，日志
`artifacts/evaluation/terminal-continuation-v18/pytest-full-venv-after-goal-exitcode-v2.log`
的 SHA-256 为 `4FBA774CEB187D57C2C27E7A50F03B25502352B395D014B97BC512293CA5C616`。
当前 E7 external join 绑定为
`artifacts/evaluation/terminal-continuation-v17/e7-rebind/release-gate-pre.8-current-20260904T030000Z-v6-bound.json`
（SHA-256 `0558A36936E82575819711351C1C9012A69607CAD0F0FF1FDC470F571C96A51E`），report-only
为 `release-gate-pre.8-current-20260904T030000Z-v6-bound-report.json`
（SHA-256 `AEA351DF5C7E74999607AF96E7C1918455D82B5CDE187C0EBAE5C952D91E66ED`）。E7 已接受
当前 parity 下的 software-only external SIM source，但总门仍为 `gate_passed=false`；现场、
REPLAY accuracy、HIL/REAL 和人工 Fly View 缺失，最终 `v0.3.0` 仍保持关闭。更早的
2026-09-03 数值仅保留在后续历史章节，不覆盖这里的当前 pre.8 指针。

ONNX Runtime 默认 provider 顺序已固定为 `TensorRT → CUDA → CPU`。只有未显式指定 provider
时，session 构造失败才会触发后缀回退；显式列表构造失败保持 fail-closed。`model-check` 与
`jetson-bench` 会记录每次尝试和最终 provider，便于在 Orin 上验证真实加速来源；本机没有
Orin/JetPack，不能把 CPU 回退结果写成现场 TensorRT/CUDA 验收。

根目录 [`configs/product-version.json`](../configs/product-version.json) 是版本单一真源：

| 用途 | `v0.3.0-pre.8` 映射 | 原因 |
|---|---|---|
| 用户、Git tag、构建目录、交付文件 | `0.3.0-pre.8` | 保留清晰 prerelease 身份 |
| CMake `project(VERSION)`、PE 数值版本 | `0.3.0` | CMake 数值版本只接受 `x.y.z` |
| Python / wheel | `0.3.0rc8` | 使用合法 PEP 440 prerelease |
| 渠道 | `prerelease` | 禁止误标 stable |

普通构建不得在 Python、CMake 或 PowerShell 里另写一套版本。若显式向构建脚本传入 `-Version`，
该值必须与 manifest 的 `release_label` 完全相同，否则构建立即失败。当前 wrapper 的主交付是
完整 portable 目录及其中的主 EXE：

```text
build/windows-v<version>-release/portable-v<version>/bin/MultiDetectGCS.exe
```

该目录中的 Qt、QML、GStreamer 和 MSVC 运行时是交付物的一部分，不能只复制 EXE。当前脚本
不创建 ZIP，也把 `QGC_BUILD_INSTALLER` 固定为 `OFF`。若后续显式启用安装器并真实生成文件，
安装器才归档为：

```text
    artifacts/qgc/windows/MultiDetectGCS-v0.3.0-pre.8-windows-amd64.exe
```

功能名称属于发布说明，不进入 portable 目录、归档或安装包名称。发布时必须补录实际主 EXE
大小和 SHA-256；额外生成 ZIP/installer 时还要分别记录各自大小和 SHA-256。旧的 v0.2.0
便携目录继续作为已验证回退基线。`v0.3.0-pre.1`、pre.2 和 pre.3 只保留为历史 prerelease；
任何历史哈希都不能映射为当前 pre.8 结果。

### 当前 pre.8 wrapper/build 复核（2026-09-03）

从仓库根目录执行 `scripts/run_current_release_gate_pre8.ps1` 的 report-only 路径（默认
accuracy/evidence 输入，显式绑定 continuation 现场预检）已实际复核成功，生成
`artifacts/evaluation/release-gate-pre.8-current-20260903T084500Z-v6-bound.json`
（17,723 bytes，SHA-256 `818A77FAB6F7C5AC6735726438CB16E447D16A7058A7C17E7611E8CC4438ED9D`）及
最新 report-only 报告
`artifacts/evaluation/release-gate-pre.8-current-20260903T103906Z-v7-alert-link-bound-report.json`
（47,716 bytes，SHA-256
`BD79B0B83FB46C2A531327B273AC5E40A8566FBF52A9F0D49780D497FEA4FF64`）。脚本退出码为 `0`，
但报告保持 `gate_passed=false` / `release_allowed=false`；这次运行只是确认输入绑定、哈希
重算和 fail-closed 阻断仍然有效。该绑定使用的最新现场 continuation 预检为
`artifacts/evaluation/field-link-preflight-20260903-continuation.json`（16,803 bytes，
SHA-256 `98E3D9BA1ECEBB6D41E61CBB54B6DE1E0B648BC86D4554C683E4D1526D2EFF98`）；报告明确记录
Ethernet `192.168.1.13/24` 不在期望的 `192.168.144.0/24`，Jetson/GR01/RTSP 均超时，
所以 `hardware_link_ready=false`，不会把现场门误报为通过。

模型审核 envelope 接入时，Windows wrapper 同样接受
`-ModelReviewBundle <model-review.json>`；无参数默认读取当前 v32 parity-valid external
SIM join/revalidation 与 v33 现场 preflight，v29/v31/provider-fallback 快照不会被隐式选用。

现场认证告警链路 bench 通过后，可在同一 wrapper 调用中追加
`-RealAlertLinkBench artifacts\\evaluation\\alert-link-bench-<DATE>.json`，让 E7 清单
直接写入 `real_alert_link` 的 hash-bound REAL 记录；参数留空时保持现有 prerelease
software-only 绑定。

当前 Release 与 portable 主 EXE 仍字节一致：`46,575,616` bytes、SHA-256
`4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`。portable 离线启动
冒烟退出码为 `0`，最新日志为
`build/windows-v0.3.0-pre.8-release/qgc-simple-boot-20260903-provider-fallback.log`
（220 bytes，SHA-256 `571A82FBF9BA2B92F4E6EF42EA83179215C52EF2AABD0A968DC2BFB7A27297E8`）；本轮未调用
computer-use。

本轮 Python 核心在上述 WGS-84 视线不确定度收口之上，又将导航速度融合的 GPS 质量下限
统一到 canonical `fix_type >= 3`、已知卫星数 `>= 6`，并保留独立 `age <= 0.75 s` 严格窗口；
QGC 仍只消费同一份证据卡片，不新增 simulator-only UI。当前 parity 文件
`configs/release-parity.json` 的 SHA-256 为
`0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`，
`core_algorithm_hash=434b86c6a8b677e60a4ae5766586b603855609e43cc0521695aa1e21995963f9`。

### 联合 SIM 聚合证据的 Windows 写入约束（2026-09-02）

联合 PX4/Gazebo/QGC 验收脚本在 Windows PowerShell 5.1 下使用有界 JSON-safe 投影：每个顶层
字段最多 `20,000` 个投影节点，集合最多 `4,096` 项，活动引用环写成 `<cycle>`，并把
`Get-Content -Raw` 附带的 `PSPath`/`PSProvider`/`ReadCount` ETS 属性复制为普通字符串。
文件 SHA-256 通过带 `FileShare.ReadWrite` 的 .NET 流计算，避免依赖 `Get-FileHash` 的模块加载。
最终 envelope 由 `Write-Utf8NoBomText` 以 BOM-free UTF-8 写入，顶层 `$null` 明确编码为 JSON
`null`，字段逐个序列化后再合并；因此新证据可由严格 JSON 解析器直接读取。

### v0.3.0-pre.8 联合 SIM/QGC/Mission 完整窗口重跑（2026-09-02）

在 v4 之后，使用同一 portable EXE 和当前源码重新执行完整的 `900` 帧联合运行，
将 Mission 观察窗口恢复到 `240 s`、setup allowance `120 s`、router `420 s`，避免
短窗口在固定翼转换或降落前过早截断。最新 source 为
`artifacts/evaluation/combined-sim-qgc-20260902T071623Z-66836/combined-evidence.json`
（250,024 bytes，SHA-256
`38E179075340A41CF040406DC89F7CF106EC1282374AD3B6376329C17CB6191B`），其独立
`combined-evidence-verification.json` 为 2,567 bytes、SHA-256
`DC06C1CC646EE37C94FC644AD59DF49074A688ECB361F69A206E475354430419`，严格 verifier
退出码 `0`。

Mission 在 `117.938 s` 内完成 `armed → MC 起飞 → FW → 7 项 INT Mission → MC 降落 →
disarmed`，最大相对高度 `25.423 m`、最大离家距离 `127.632 m`；原生 QGC fixed-wing
`videoContent` 抓帧、transport binding、AUTO Mission 截图、动态端口和 Docker 清理均有
同一运行目录的证据。新的 SIM envelope 为
`artifacts/evaluation/release-mode-sim-20260902-pre.8-combined-qgc-v5.json`（579 bytes，
SHA-256 `7434FE3E3C47A947B58C53F49AE1E083CEFBF871F3B03A06A0417E21EFBE6493`），对应 E7
清单 `artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v5.json`
（10,879 bytes，SHA-256
`1340AC8CCCB62CB02F4F82B20DBAEA8C570353C10C18D719D951A430DD46402A`）。固定时间
`2026-09-02T07:30:00Z` 的 report-only/strict 复核为 22,968 bytes、SHA-256
`E8051A6C7BD68DD793ACAB6B77415BF2D44915C5474952D23F95476CFE18C847`，退出码 `0`/`2`。
这些文件只更新 software-only SIM/QGC/Mission provenance，不关闭现场硬件、室外 GNSS、
人工视觉、HIL/REAL、REPLAY 精度或最终 `v0.3.0` 发布门。

### 台式机现场链路最新复探（2026-09-02）

2 个样本的只读 `ProtocolChecks` 报告
`artifacts/evaluation/field-link-preflight-20260902-154000-continue.json`（11,758 bytes，
SHA-256 `1F9A30F4407C1B31435F57155D675CB7BD09EC781BB3734E848D81BB4904F4FC`）确认当前
Ethernet=`192.168.1.13/24`，期望 `192.168.144.0/24` route 不存在，Jetson SSH、GR01
MAVLink、相机 RTSP 均超时；`hardware_link_ready=false`。该结果已绑定 E7
`artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v7.json`（SHA-256
`1E16B731CBF6E9252986210266785CE68A505E380065978699A7A421B00A58D2`）和 goal acceptance
`artifacts/evaluation/multidetect-goal-acceptance-20260902-combined-qgc-v7.json`（SHA-256
`082E33BEA216460271BDB44435AC6281A1F5E3F3C3F0FEC2BDDD03B9E95CD2D1`）。软件门保持通过，
现场硬件、室外 GNSS、HIL/REAL 和人工视觉门继续 pending。

### external runtime + live-AI provenance（历史 parity 快照，2026-09-02）

三机型 external runtime/AI join 曾绑定到当时的 goal acceptance：
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-continue-v2.json`
（5,407 bytes，SHA-256
`1F834FC5A86409E3BD338B349C9D3FC188D4FEDDEBB725E1CCBAB1DFB99722B2`）和
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-combined-qgc-v7.json`
（543 bytes，SHA-256
`206E667E1996C4E530BF5988E85F396E1824A407E565B97D445963AF4F9A49A0`）均由 verifier 重算
通过。对应总验收为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-combined-qgc-v7.json`
（39,850 bytes，SHA-256
`082E33BEA216460271BDB44435AC6281A1F5E3F3C3F0FEC2BDDD03B9E95CD2D1`），核心测试 `2421`、
QGC custom `66/66`、`software_status=pass`。这些是无物理输出的 software-only SIM 证据；
现场硬件、室外 GNSS、HIL/REAL 和人工视觉仍须单独验收。

### 当前 pre.8 goal acceptance 与 external join 刷新（2026-09-02）

由于 canonical safety parity 在手动 watchdog 收口后发生变化，旧 external join 的摘要被
重新计算并 fail-closed。当前 source-bound join 为
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-manual-watchdog-v2.json`
（5,407 bytes，SHA-256
`6701803509C4DE7322B553034B6ABAC72D4C04836C6289C85B51C61F88652770`），独立 revalidation 为
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-manual-watchdog-v2.json`
（544 bytes，SHA-256
`A1C653C53E71F22186D2216858628575C3020200E2949C38653D8996510078D0`）；两份均
`source_recomputed=true`、`gate_passed=true`。

最新完整软件验收为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-manual-watchdog-v2.json`
（39,946 bytes，SHA-256
`13467799B7924BED99E72DAFCEA214FBA4FFBE96C246C160B4C37772DDCE6929`），核心测试 `2431`、
QGC custom `66/66`、`software_status=pass`。对应 E7 清单
`artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v2.json`
（10,481 bytes，SHA-256
`AAC4FDA23C6F47CC23D856F8B5C06F59E164B4C3222538B9B0A5D2F847D05572`）和 report-only 报告
`artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v2-report.json`
（21,863 bytes，SHA-256
`040EC28064A4C4BCA17BA7273DB1E929B661E029A7786F7CC62D8B9846AEB848`）保持总门关闭；
现场设备、室外 GNSS、人工视觉、HIL/REAL、REPLAY 正样本和最终 `v0.3.0` 仍未验收。

### v0.3.0-pre.8 联合 SIM/QGC/Mission v4 通过（2026-09-02）

当前 portable EXE 在严格的同 PID QGC 窗口回退生效后完成 900 帧 Docker PX4/Gazebo/QGC
联合验收。顶层 source 是
`artifacts/evaluation/combined-sim-qgc-20260902-hwnd-fallback-v4.json`（249,689 bytes，
SHA-256 `8F90EAB7232A29382451CF99BB5B3CF0E4D44674E9BC4B027C0962120C46D692`），
`gate_passed=true`；独立 verifier 为
`artifacts/evaluation/combined-sim-qgc-20260902T030210Z-77524/combined-evidence-verification.json`
（1,941 bytes，SHA-256
`8913C015977B1C879E07A76A48E9F9D2AD86E0F2F8784F1D81AC68FAE9C71055`），退出码 `0`。

验证覆盖集成 SIM、VTOL attach、AUTO.MISSION、armed/in-air、MC→FW→MC、landed/disarmed、
900/900 RTSP/传感器帧、动态 loopback 端口、Docker 清理和 13 条 Mission journal
hash-chain 记录；Mission 持续 `116.078 s`，最远距离 `142.571 m`、相对高度 `26.587 m`。
QGC 原生 `videoContent` fixed-wing QQuickItem 抓帧和 transport binding 通过，工具栏
indicator/drawer 机器烟测通过。桌面 BitBlt/GPU ROI 诊断与人工视觉仍保持
`not_claimed`，该运行只关闭 software-only SIM/QGC/Mission 子门，不改变现场 GNSS、真实
Pixhawk/Jetson、HIL/REAL 或最终 `v0.3.0` 发布门。

当前 v4 source 的 SIM release-mode envelope 为
`artifacts/evaluation/release-mode-sim-20260902-pre.8-combined-qgc-v4.json`（562 bytes，SHA-256
`65B96C6DF3BC66BD77B13D56BE8D5019210B6C52C6638BECA0162980DE9D7CC8`）。带
`--require-release-mode` 的独立 verifier 输出
`artifacts/evaluation/combined-sim-qgc-20260902T030210Z-77524/combined-evidence-verification-v4-bound.json`
（2,535 bytes，SHA-256 `5BDAB531DD120645BFE5AF4A8D9816A222EE21B458C020F3EECBA48FE7E1D415`），
`gate_passed=true`；这仍只属于 software-only SIM 绑定，不改变现场 GNSS、真实 Pixhawk、
HIL/REAL、人工 Fly View 或最终 `v0.3.0` 门。

### 当前 E7 预发布清单绑定（2026-09-02，pre.8）

当前 v4 source 已写入统一 E7 清单
`artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v4.json`（10,852 bytes，
SHA-256 `EAACCA3F40A4B9A6734C550EFFC435A0A13B2F1FC4E08FA12FA0C2F613B66D9F`）。清单内
SIM/QGC/Mission、S4 safety、三机型 scenario matrix 和 PX4 airframe transaction 子门均为真，
同时保留 REPLAY accuracy 与严格现场链路的 fail-closed 状态。

固定时间 report-only/strict 输出为
`artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v4-report.json` 与
`...-strict.json`（均 22,896 bytes，SHA-256
`F7AEC221A979F03D0AED6FC3B5E983B6458A84CE2C89FCBCB9439546CFC22F19`）；退出码为 `0`/`2`，
两者都保持 `gate_passed=false`、`release_allowed=false`。REPLAY 部署/精度、现场
`hardware_link_ready=false`、四模式 `not_claimed`、prerelease channel 和最终 release surfaces
仍是阻断项；该清单不会把 software-only SIM 或诊断报告写成硬件、室外 GNSS、HIL/REAL 或
人工 Fly View 通过。

QGC 主窗口屏障在 Qt 生命周期切换导致 `Process.MainWindowHandle` 短暂回零时，会从**同一 PID**
的可见、非最小化、非 DWM-cloaked 顶层窗口中恢复句柄；只有唯一候选才接受，零候选或多候选
仍保持 fail-closed。该恢复只写入 stage-progress 诊断旁路，不使用猜测坐标、不改变飞控或
QGC 控制路径。

脚本另外写出 `combined-stage-progress.jsonl` 与
`combined-evidence.json.serialization-progress.jsonl` 两个只读旁路，用于定位哈希、清理和
逐字段投影阶段；它们不参与 QGC/飞控控制，也不改变发布门。台式机复核
`artifacts/evaluation/combined-sim-qgc-20260902T022832Z-50960/combined-evidence.json`
（31,347 bytes，SHA-256
`EC3B88DC55CDCB460CEF80ACA3D4C672C71AA12A608F098F4F11DE22CEBF8756`）仍记录
`gate_passed=false`，因为后续阶段未取得 QGC 主 HWND；该历史文件按原始字节保留，新的运行
使用无 BOM 写入器。它不关闭真实 Jetson/相机/室外 GNSS/Pixhawk、HIL/REAL 或人工 Fly View 门。

同日的只读现场链路基线为
`artifacts/evaluation/field-link-preflight-20260902-next.json`（4,397 bytes，SHA-256
`5223C913DF97BA3F7E48FF4D0BB28C020D43302F93BC5EFF600CEA2DC9D0C627`）：当前 Ethernet
仍是 `192.168.1.13/24`，Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760` 和
相机 `192.168.144.108:554` 的 TCP/协议探测与三个 route probe 均失败；本机 metadata/QGC
UDP `14580/14581` 可绑定。普通诊断 wrapper 与 `--allow-unready` verifier 退出 `0` 只代表
结构有效，报告仍为 `hardware_link_ready=false`/`offline_pending`；接通飞机 Ethernet 后
需用 `-RequireReady` 重新生成现场证据。

随后同一台式机又保存了双样本 watch 与严格单样本对照：
`artifacts/evaluation/field-link-preflight-20260902-continue.json`（11,755 bytes，SHA-256
`B779A8D4CCD3FAD11DEDDF03E9E691E0C53552E82381440490814058E14850EB`）的两个样本均未通过
Jetson/GR01/RTSP、协议或 `192.168.144.0/24` route，`hardware_link_ready=false`；
`artifacts/evaluation/field-link-preflight-20260902-continue-strict.json`（4,401 bytes，
SHA-256 `2D1F71C74B7B5F13516C29D3C8BF900A3B3F3B54D8EDAB9E0463595749B5D1F6`）在
`-RequireReady` 下 wrapper/verifier 均退出 `1`。两份都只是接线前事实，不会关闭现场 GNSS、
真实 Pixhawk、HIL/REAL 或最终 `v0.3.0` 门。

### 2026-09-02 当前软件验收绑定（pre.8）

REPLAY 评估器的空基线 fail-closed 修复已纳入
`artifacts/evaluation/multidetect-goal-acceptance-20260902-replay-guard-v8.json`
（39,850 bytes，SHA-256
`C0CF30A37C028B3E526C0068EE10DC5A8DBDAE276F2EBCD3C6FC7B9BBD849391`），核心测试 `2423`、
QGC custom `66/66`、软件 `pass`、硬件 `offline_pending`。同一 portable EXE 未发生字节变化；
空基线报告 `artifacts/replay-sets/sim-gazebo-2026/accuracy-report-20260902-precision-gate.json`
仍以 undefined precision/recall 保持 `gate_passed=false`。

该报告已进一步绑定到当前 pre.8 E7 清单
`artifacts/evaluation/release-gate-pre.8-current-20260902-replay-guard-v8.json`
（10,913 bytes，SHA-256
`8B62437E85583F244047F4CFA18D34C724F5E0C4C51DA39A66C01096EF1CE6E9`）及固定时间
report-only 输出
`artifacts/evaluation/release-gate-pre.8-current-20260902-replay-guard-v8-report.json`
（23,034 bytes，SHA-256
`1E5B1FAFA11D8058DC0C21A3FF0435CD92E7D30CF1BCDEE40C7D79DBBD8E5823`）。复核退出码为
`0`/`2`（report-only/strict），`gate_passed=false`、`release_allowed=false`；
这次只收口 replay 精度分母保护，不改变 portable EXE。

最新一轮只改 Python 共享质量门，没有改变 QGC portable 二进制：SIM AI 的
`SimulatorTelemetrySnapshot.gps_quality_valid` 与返航校验现在统一 hAcc `<=6 m`、vAcc `<=10 m`、
GPS age `<=1.5 s`，旧的单一 accuracy/age 配置也会在 RETURN 前按 canonical policy 收窄。新增的
边界回归、全量 Python 回归和 QGC custom 回归均通过；当前快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-ai-gps-accuracy.json`（31,407 bytes，
SHA-256 `074DE9C2C8551C3B3CEF7E8462BDF05DFD2CD8AFC70A598BFDF800FA8468F03E`），核心测试 `2,397`、
QGC custom `66/66`。对应 parity 文件为 `configs/release-parity.json`（3,317 bytes，SHA-256
`6D0CDBD3D376FBE1AF46A51C721AF3785528CA6BF22016DCD4216979769D7FDF`），其中
`core_algorithm_hash=6c2d186e8b6c31330b16fae096c4d90302797bdf5d697e28ff4efbc55fbdc06d`。

随后在 Python 测距层加入 direct-range 的有限时间、来源唯一性和向下射线 fail-closed 门，
并重新执行全量质量门。最新总验收快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-direct-range-safety-pre8.json`
（39,808 bytes，SHA-256
`CEEBFAF6B0D0006C64E337A98FB9E30D153FCB1127C4B8A9CE38D0AFCFAC295`），核心测试 `2,421`、
QGC custom `66/66`、软件状态 `pass`。这次变更未触碰已验证的 QGC portable EXE；最新 E7
清单为
`artifacts/evaluation/release-gate-pre.8-current-20260902-direct-range-safety.json`
（10,870 bytes，SHA-256
`87B88E5FAF2AB25435CD74272B4F9EB9F52D5F24FFC9D57F51523D79A11E7A6E`），固定时间 strict
复核仍退出 `2`，`release_allowed=false`。现场硬件、室外 GNSS、REPLAY 精度、HIL/REAL 和
人工 Fly View 门未改变，版本继续是 `v0.3.0-pre.8`。

重新绑定的 E7 清单和固定时间 report-only 报告为
`artifacts/evaluation/release-gate-pre.8-current-20260902-ai-gps-accuracy.json`（11,454 bytes，
SHA-256 `47C5C9DA6E0F00B8B6CF4F000D1B693AEE71E632847ABD1F7C6380F06AABFE42`) 与
`artifacts/evaluation/release-gate-pre.8-current-20260902-ai-gps-accuracy-report.json`（25,301 bytes，
SHA-256 `42A18B5929122FFB4F09ABC5638757F44F99587054D2827B2D2C16BEDB4A24F1`)；两者均明确
`gate_passed=false`、`release_allowed=false`。现场 Ethernet/Jetson、室外 GNSS、真实 Pixhawk、
HIL/REAL 与人工 Fly View 尚未完成，所以这次 Python-only 收口不会提前放行最终 `v0.3.0`。

随后在同一台式机上完成了严格现场预检 v4 与 E7 绑定刷新：现场报告
`artifacts/evaluation/field-link-preflight-20260902-continue-v4.json`（4,396 bytes，SHA-256
`0438D61B311BBA94150CF59F580DBC4A29E6B31999310E33238A1E52F1A3506C`）的三个远端端点和
`192.168.144.0/24` route 均未就绪，本机源地址仍为 `192.168.1.13`，严格退出码 `1`。
绑定后的总验收为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-field-preflight-v4.json`
（31,777 bytes，SHA-256
`250824C312DD0CD8326721C0B27B3B10F05F53E9644FA82CC00EC87F668905F7`），核心测试 `2,397`、
QGC custom `66/66`、软件 `pass`、硬件 `offline_pending`。对应 E7 清单
`artifacts/evaluation/release-gate-pre.8-current-20260902-field-preflight-v4.json`
（SHA-256 `0E8DB97FDA3157967FB567F8EA5DB91088C52BD2D45FB345A5A04A5BA9CC7AE3`）的固定时间
report-only/strict 复核退出码为 `0`/`2`；这只更新 provenance，仍不关闭现场、HIL/REAL、
室外 GNSS、人工 Fly View 或最终版本门。

此前 GPS contract 子门的 goal acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-contract-v4.json`
（SHA-256 `1EB9D735CEB9FD9400F780074278952EFF81E20990AD83769E2AF6BD11620002`）；
该快照收集核心测试 `2271`（`2268 passed / 3 skipped`）、QGC 自定义回归 `66/66`，并验证
GPS/RANGE_STATUS 新鲜度契约、QGC 原生 Fly View toolbar 诊断行、S4 SIM 安全证据、现场链路摘要
一致性、协议 self-test、QML parse 和 portable smoke。软件状态仍为
`pass_with_environment_gap`、硬件状态为 `offline_pending`；现场、室外 GNSS、HIL/REAL 和人工
Fly View 仍未声明通过，旧快照继续保留作历史 provenance。对应固定翼子证据为
`fixed-wing-software-acceptance-20260901-gps-contract-v4.json`（SHA-256
`1EE64A535A1C64FD41A4EA02F3164F5A62514B0ADF9006DF5CBE0C5D9215FF4C`），SIM 安全子证据为
`simulator-safety-acceptance-20260901-gps-contract-v4.json`（SHA-256
`021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`）。最新只读现场预检为
`artifacts/evaluation/field-link-preflight-20260901-v3.json`（SHA-256
`C5C4F0405FE9D61E373550FBEE9DB929580838FAEF417BCCEB99E11D19AF0D05`），三采样仍显示远端
端点超时，`hardware_link_ready=false`。

随后补入的 PX4 Mission durable journal 不改变 QGC 二进制，但把同一 Mission transaction 的
`MISSION_REQUEST_INT`、ACK、readback、commit 和 start-permit checkpoint 以 hash-chain JSONL
持久化；当前全量源码回归已收集 `2296` 项（`2293 passed / 3 skipped`）。最新短超时现场复检为
`artifacts/evaluation/field-link-preflight-20260901-journal-followup.json`（SHA-256
`B817AA9CD558EA13255F8B459BE42913DB2F5F63C29A7D88FB6F31C71477700A`），三个远端端点仍超时、
本机 `14580/14581` 可绑定，故硬件、室外 GNSS、HIL/REAL 和人工 Fly View 门继续关闭。

2026-09-01 的最新 900 帧联合 SIM 重跑已经把新的 Mission journal、Gazebo 传感器发布器稳定
屏障和 QGC 精确客户端捕获重试纳入同一证据链：
`artifacts/evaluation/combined-sim-qgc-20260901-continue-recovery-v9/combined-evidence.json`
（333,051 bytes，SHA-256
`75265742413146EA78AB88CCBD30B22ACFCC0C5DC7EC48780F4A9D25F649F7FB`）和独立复核
`.../combined-evidence-verification.json`（SHA-256
`18FDB7E6E5302229D2244075C26998D8D66E7C7EEBD4FCFB24A6A35111761960`）均通过。source 的
release-mode envelope 为
`artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v3.json`
（SHA-256 `ACDFB988D7BE669FFEEC4846BDA6F66837B7892CF402DA1D25B01DB84D92A045`）。
绑定后的 pre.8 E7 清单/报告为
`artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v5.json`
（SHA-256 `629237034B84498537C07CB7885BCB6F28D20FCD4E8EEA4CAD5D43ACD9618A6A`）/
`...-report.json`（SHA-256
`59C6A45CCC08084683485E39F3D38EFE7869645640C51E7427C41E8ADF5958BD`）。该重跑关闭
software-only SIM 子门；现场、真实硬件、室外 GNSS、HIL/REAL 和人工 Fly View 仍保持
`offline_pending`/`not_claimed`，最终版本继续不是 `v0.3.0`。

随后重新执行 goal-acceptance 并绑定最新只读现场预检：
`artifacts/evaluation/multidetect-goal-acceptance-20260901-combined-qgc-v5.json`
（11,200 bytes，SHA-256
`E4690933BC325D1CE2828DDAAC63C65689832AB8E1AFD07AB42AA9D397DE2447`）。该快照收集核心测试
`2315` 项、QGC 自定义回归 `66/66`，脚本退出码 `0`，软件/硬件状态仍为
`pass_with_environment_gap` / `offline_pending`；它绑定
`artifacts/evaluation/field-link-preflight-20260901-final-continue.json`
（SHA-256 `59F85CEF567B198E12464B306E74DC36177CB5441F997B41A9034F3728025D54`），
Jetson/GR01/相机端点继续超时，因此这次刷新只更新现场事实，不改变 REAL/HIL 或最终发布门。

当前开发线随后以故障覆盖和联合 SIM 强制绑定重新执行总验收，权威快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-fault-coverage-v5.json`
（36,169 bytes，SHA-256
`7DC28AA4A36204C82C58680E7637C873859B93F1298762F6BC3EB2A072B72C41`）。该快照收集核心测试
`2337`、QGC custom `66/66`，软件状态为 `pass`，并通过独立 verifier 绑定
`artifacts/evaluation/combined-sim-qgc-20260901-continue-recovery-v9/combined-evidence.json`
（333,051 bytes，SHA-256
`75265742413146EA78AB88CCBD30B22ACFCC0C5DC7EC48780F4A9D25F649F7FB`）及其
`release-mode-sim-20260901-pre.8-combined-dynamic-ports-v3.json` envelope
（730 bytes，SHA-256
`ACDFB988D7BE669FFEEC4846BDA6F66837B7892CF402DA1D25B01DB84D92A045`）。同一快照绑定的现场
预检 verifier 已验证报告结构，但 Jetson/GR01/相机仍不可达，`hardware_link_ready=false`、
`hardware_status=offline_pending`；因此该刷新只关闭 software/SIM 子门，不升级室外 GNSS、
真实 Pixhawk/执行器、HIL/REAL 或人工 Fly View。
同一 source 还已重绑到上一轮
`artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v5-bound.json`
（SHA-256 `C5892E0515D1C6C26199E35CBC80CB405880D5D6C35A0AB045F70EAA7E121393`）；报告严格模式
退出码为 `2`，保持 `release_allowed=false`，因为现场链路、REAL/replay-set、人工视觉/硬件和
prerelease 渠道仍未满足。

2026-09-02 的 v6 总验收把当前日现场预检重新绑定到同一 pre.8 source：
`artifacts/evaluation/multidetect-goal-acceptance-20260902-fault-coverage-v6.json`
（36,169 bytes，SHA-256
`0F59CEC1C31B2B30669598E064F71F217051E86BBD300304BB4B3CA3A4402AE1`），脚本退出码 `0`，
核心测试 `2337`、QGC custom `66/66`、软件状态 `pass`。现场文件为
`artifacts/evaluation/field-link-preflight-20260902-continue-v1.json`（3,038 bytes，SHA-256
`64C06112A967BC0C3BABE904C0588143C182F2A221262A0D97C062E6077119AD`）；结构校验通过，但
Jetson/GR01/相机不可达、`hardware_link_ready=false`，因此不改变室外 GNSS、真实 Pixhawk、
HIL/REAL 或人工 Fly View 状态。对应 E7 清单为
`artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v6-bound.json`
（SHA-256 `48A3D463A05FC30896B8651CB698E88FF242700A56B6CFEDFE83CB34BD78F648`），report-only
退出码 `0`、严格退出码 `2`；v5 证据保留作历史 provenance。

现场预检现支持可选的本机路由核验。执行
`scripts/run_field_link_preflight.py --require-route --expected-source-cidr <aircraft-cidr>`
后，报告会在 `route_probes` 中记录 Jetson、GR01 和 RTSP 端点的本地源地址；检查通过
操作系统 UDP route selection 完成，不发送应用数据。带 `--expected-source-cidr` 时，源地址
必须属于显式机载网段，VPN/TAP 或迁移后错误网卡会让 `route_checks_passed=false`，从而保持
`hardware_link_ready=false`。当前台式机报告
`artifacts/evaluation/field-link-preflight-20260902-route-v1.json`（4,396 bytes，SHA-256
`21DAD57EA9CF8AD1FA9996F8110193BEA3535E270F7E87C5D7A100A4BA699B13`）显示三个端点都从
`192.168.1.13` 出口，未进入期望的 `192.168.144.0/24`，严格预检退出 `1`；接线后重新
运行同一 route-enabled watch，再把通过的报告用 `--FieldLinkPreflight` 绑定总验收。
当前脚本已把 route 摘要同步写入总验收；最新绑定为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-route-v2.json`（29,706 bytes，
SHA-256 `C7DECB029209C4CA6D05CB74D1B951E89811424C884FA0F051D1F5378E87BEE4`）。

同日保存的最新三样本 route/protocol 基线为
`artifacts/evaluation/field-link-preflight-20260902-desktop-continue.json`
（16,806 bytes，SHA-256
`98C6039B2E6988E066DA82C549530AB25C160DE6EFDAA0FEC50F21396D2A68BC`）。报告结构 verifier
在诊断模式退出 `0`，但三组远端端点均超时，route source 仍为 `192.168.1.13`，
不属于 `192.168.144.0/24`，故 `hardware_link_ready=false`；接线后应在同一网卡上
重新运行 wrapper，并把通过的报告绑定到总验收，不能复用此离线基线关闭硬件门。

该基线已绑定到完整总验收
`artifacts/evaluation/multidetect-goal-acceptance-20260902-desktop-continue.json`
（31,412 bytes，SHA-256
`9E7347F288151F53245DD103242993183C38F6ED363079F4C53C63A3EC2A9E66`）。本次收集核心测试
2,366 项、QGC 自定义 66 项，软件状态为 `pass`；现场 verifier 对未就绪报告返回 `1`，
总验收继续保留 `hardware_status=offline_pending`，不改变 HIL/REAL 或最终版本发布条件。

随后按台式机当前网络状态重新执行的最新 goal-acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-network-refresh.json`（SHA-256
`7D070D0F49547D0569308959E8184E944FEDDAB7831C4A13B9BF659D2CC18458`），收集数为 `2167`
（`2166 passed / 1 skipped`），QGC 自定义回归 `66/66`。物理 `Ethernet` 为 `Up` 但只有
`192.168.1.13/24`，飞机 `192.168.144.0/24` 没有路由，`Ethernet 2` 为断开的 TAP，
所以 field-link、GNSS、真实 Pixhawk/执行器、HIL/REAL 和人工 Fly View 仍为
`offline_pending`/`not_claimed`；该刷新不改变 `v0.3.0-pre.8` 或最终发布门。

2026-09-02 又加入了 `scripts/prepare_field_link.ps1 -Elevate`，并重新执行总验收：
`artifacts/evaluation/goal-acceptance-20260902-033125-field-link-uac-v3.json`
（31,491 bytes，SHA-256
`1292D7CB4A059DA47CA8BCCFC723855284998ED2D05627D84571C9334F627030`），退出码 `0`，核心测试
`2353`、QGC custom `66/66`、软件状态 `pass`，硬件仍为 `offline_pending`。对应 HIL envelope
为 `artifacts/evaluation/release-mode-hil-20260902-field-link-uac-v3.json`
（539 bytes，SHA-256
`363954C63B419A1F479A4452A6617EDD708D959E3883E235A396169BA2648F29`），pre.8 E7 清单为
`artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-uac-v3.json`
（10,888 bytes，SHA-256
`2AB23A691DD3B7B54899760C01A2445D207F5310693EA4382DB7A53E0C08B5D3`）。固定时间 report-only/strict
报告均为 22,782 bytes、SHA-256
`672533CA780DED8A37183FC8D3CEC267AA3CA586D9B3F3134252FA911DD28973`，退出码 `0`/`2`；现场
route、室外 GNSS、HIL/REAL 物理和人工视觉门仍未通过。

同轮对当前 `192.168.1.0/24` 做的有界协议发现保存为
`artifacts/evaluation/field-link-discovery-20260902-033839-desktop-uac-protocol.json`
（8,437 bytes，SHA-256
`C5BB3A87128F452F8C9A7269CCDE44376AF77889B90AD5373EED61EED9A1F2E5`）。4 个 SSH banner 主机
中没有 `5760/MAVLink` 或 `554/RTSP` 命中；它们仍是普通局域网诊断结果，不能替代显式 Jetson、
GR01 和相机地址。

总验收脚本在接通现场后可以直接绑定已保存的预检报告：

```powershell
Set-Location D:\Projects\Multi-Detect
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_goal_acceptance.ps1 `
  -FieldLinkPreflight .\artifacts\evaluation\field-link-watch-<stamp>.json `
  -RequireFieldLinkPreflight
```

`-FieldLinkPreflight` 的绑定步骤不重新连接 Jetson、GR01 或相机，只重算报告 SHA-256、字节数和 readiness；
总验收脚本原有的 ICMP 状态记录仍会照常执行。
`-RequireFieldLinkPreflight` 要求 verifier 返回 `hardware_link_ready=true`，否则脚本立即退出非零。
省略两个参数仍保留台式机离线软件验收语义，不把 ICMP 或历史报告升级为硬件门。

### 现场接线前：配置签名控制密钥

签名 live-control 还需要两个 Windows **当前用户**环境变量。为避免手工复制、弱密钥和
把密钥写入仓库，使用仓库内的幂等配置器：

```powershell
Set-Location D:\Projects\Multi-Detect
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\provision_multidetect_operator_keys.ps1
```

该脚本只写 `User` scope：`MULTIDETECT_OPERATOR_KEY` 使用至少 32 个 UTF-8 字节的
系统随机 HMAC 材料，`MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX` 使用固定 32 字节（64 个
十六进制字符）的 MAVLink2 signing key。已有合法值默认复用；只有明确传入 `-Force` 才会
同时轮换。脚本会从用户 scope 读回并校验长度/格式，输出操作状态和 SHA-256 指纹，**不输出
原始密钥**。需要机器可读结果时加 `-PassThru`；执行后重启终端和 QGC，让新进程继承环境：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\provision_multidetect_operator_keys.ps1 -PassThru
```

配置器只解决凭据准备，不代表 Jetson、室外 GNSS、Pixhawk、执行器或 HIL/REAL 已连接；接线
后仍必须用 `run_field_link_preflight.py --require-all` 采样，并把通过的报告绑定到总验收。

配置器与验收序列化修复后的最新 goal acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-key-provisioning-v8.json`
（23,276 bytes，SHA-256 `094ECA0E16A33D4C3F81171B6AF2A7366BA296FDE1491DF2912247ED16A464CA`）。
该快照不带 `-AllowMissingOperatorCredentials`，核心测试收集 `2318`、QGC custom `66/66`，
两项用户级签名凭据 readiness 为 `true`；现场 `hardware_link_ready=false`、硬件状态仍为
`offline_pending`，未提升任何真实设备、室外 GNSS、HIL/REAL 或人工视觉门。

随后收紧现场会话的目标身份绑定：一个验收窗口必须对应一个选定目标，GPS/RANGE 的带 ID
样本集合必须完全相等且唯一。分离记录出现部分交集或多目标集合时，验证器会在
`target_binding.cross_record_mismatches` / `target_binding.multiple_target_ids` 保留诊断并
fail-closed，而不是把不同目标的坐标和距离合并。对应的最新总验收快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-target-binding-v9.json`
（23,268 bytes，SHA-256 `7CC6B274EE6CA188A718B7DBBD306A514A92750882D5FC7D75C19B61056A6538`），
核心测试 `2319`、QGC custom `66/66`，软件门通过；现场 `hardware_link_ready=false`、
`hardware_status=offline_pending` 继续保持，未提升 Jetson、室外 GNSS、Pixhawk/执行器、
HIL/REAL 或人工 Fly View 状态。

最新源码回归还验证了 perception bridge 的共享入口：
`SimulatorPerceptionBridge(telemetry_bridge=...)` 的 `feed_result()` 委托唯一的
`SimulatorTelemetryBridge.ingest_perception()`，使绑定 bridge 的感知帧统一经过 heartbeat、
armed、target-lease、watchdog 和 authority 门；future/stale/out-of-order/malformed 拒绝会在
返回 runner 前清空目标四元组。对应完整 pre.8 goal-acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai-v3.json`（SHA-256
`A49DDB605FD90AA04BBFCA7185ECB600FACA52E3BAF3185F262C2B9DE6F3D560`），核心测试 `2178`、
QGC 自定义回归 `66/66`，脚本退出码 `0`；`hardware_link_ready=false`、
`hardware_status=offline_pending`、人工 Fly View `not_claimed` 继续保留。该 Python-only
修复同步更新 `configs/release-parity.json`（文件 SHA-256
`C3A5ED43183DF66419C47AF023512EEF3389ECC1FF4D2A791DBCB1279A4EAF9E`，当前
`core_algorithm_hash=71e59bb128a652d67827aa3da615af4b8f8af8f0ad8a6358cf7acc6ab16bcd63`），
不需要重编 portable QGC，主 EXE SHA-256 仍为
`61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`。

随后将同一 `RANGE_STATUS` 新鲜度门下沉到 Python `SimulatorPerceptionBridge`：默认
`maximum_range_age_s=1.5` 秒，`RangeSolution` 的数据年龄和 `RangeStatusMessage` 的源帧时间
均校验有限/非负/非未来/不超时。过期、未来或显式缺失源时间的输入只保留视觉 track，清空
距离、方位和测距置信度；负数或非有限年龄按 malformed 计数并拒绝。该接缝修复后的完整
goal-acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai-v4.json`（SHA-256
`D4B0EE2E7F274B0904DE2CDB60AC42DA4195CFAFE31D1774418A8180E46DE5CF`），核心测试 `2183`、
QGC 自定义回归 `66/66`，脚本退出码 `0`；`software_status=pass_with_environment_gap`、
`hardware_status=offline_pending`、`hardware_link_ready=false` 和人工 Fly View
`not_claimed` 保持不变。canonical parity 文件已同步为 SHA-256
`CB51D7CDD1AC0CA0D52F21241C88A2630B50D18DAFD88A0B9D4FE01185728BD2`，当前
`core_algorithm_hash=6f224eb199fd53979648f477e364f30c318c303b6a97d303eae548948f9290f0`；这是
Python-only 改动，主 EXE SHA-256 仍为
`61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`，无需重编 portable QGC。

SIM AI 返航门随后加入独立 `gps_observed_at_s` 新鲜度约束：年龄必须在 `1.5 s` 内且不能来自
未来，缺失/过期/未来 GPS 均进入 `return_gps_quality_insufficient`。共享闭环脚本与 fresh-boundary
回归通过；最新总验收为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-field-recheck-v5.json`（SHA-256
`7EAE02CC318160650673508692C8D5F5C6636ADBADA1ACED2BBF098661FAE70`）。为绑定这条共享 AI 路径，
`src/multidetect/simulator/ai_executor.py` 已纳入 canonical `core_algorithm` parity；
`configs/release-parity.json` SHA-256 为
`93269062DFCD4D2EA01E31AA8EE49F0FB4A074B0F8FF7DC63AB3979AD4AD9F70`，
`core_algorithm_hash=da5fe6ff7d1455b85c51bf975e14f2da007e603de5aed69b93470cd3a31de006`。

返航 GPS 门改动后的完整验收重新绑定为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-return-freshness-v6.json`
（SHA-256 `B43DC058C34A7ACEC7E489DEF0BFC55FD8D9A7348FB30D2F5B3F54B256F4189C`），核心测试
`2188`、QGC 自定义回归 `66/66`，脚本退出码 `0`。现场链路报告结构复核通过但
`hardware_link_ready=false`，因此只确认软件回归，不提升 Jetson/室外 GNSS/Pixhawk、HIL/REAL
或人工 Fly View 状态。

随后为 E7 `real_field_link` hash-bound 发布绑定重新执行的最新 pre.8 软件门快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-real-field-link-gate-v2.json`
（SHA-256 `7075D414DEF8A809AFEB7FB5BDD41DFE8008F80C4FE08C0228596546D01386C0`）；核心测试收集
`2141`、QGC 自定义回归 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`。
绑定的三采样 watch 仍为 `hardware_link_ready=false`、verifier exit `1`，所以该证据只证明失败
状态被保留，未升级 Jetson、GNSS、Pixhawk、执行器、HIL/REAL 或人工 Fly View 门。

2026-09-01 Docker Desktop 后端恢复后，使用固定 digest 的
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
完成三种机型的 external runtime gate。VTOL、fixed-wing、multirotor 的 runtime evidence
分别是 `artifacts/evaluation/simulator-docker-acceptance-20260901-continue-vtol.json`（SHA-256
`391BDA5D7832F08174B8260D9B1B43ADC6BB5FB3E67E82AC5EE6F1859905E16D`）、
`artifacts/evaluation/simulator-docker-acceptance-20260901-continue-fixed-wing.json`（SHA-256
`FC20E7A241E53A7BBE82B4498D86BD06D2AA614D4AD44A1AD57464A3A11F4A22`）和
`artifacts/evaluation/simulator-docker-acceptance-20260901-continue-multirotor.json`（SHA-256
`D3CA94B17C3A1461E059D078020BFA1010930257A76A18CE28FA8935040BD75F`）；三份均为
`gate/runtime/telemetry/cleanup=true`。同一容器链路再执行 `--exercise-target` 的 live AI
acceptance，三份 evidence 均为 `ai_policy_gate_passed=true`、`target_policy_gate_passed=true`、
`ai_steps=2`、`armed_observed=true`、`decision.action=track`，并完成 disarm/清理：
`artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-vtol.json`（SHA-256
`C60E759BC99E5BF9D5190CE711B9B93F219880A7A7ED2534D797E394A3CC1B57`）、
`artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-fixed-wing.json`（SHA-256
`99FBF23B71A94A21A9F6EB34A43D7737FEC651DAF5E3E267E837C7A0F0CB4032`）和
`artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-multirotor.json`（SHA-256
`0633659951E05C9BD5384251AE95D7D834FEE076CE782D6197CF79167A6D032A`）。这些是
disposable headless SIM 子门，合成 truth-free perception 不提升人工 Fly View、相机精度、
Jetson/室外 GNSS、真实 Pixhawk/执行器、HIL/REAL 或最终 `v0.3.0`；版本仍为 `v0.3.0-pre.8`。

六份 disposable evidence 还可用仓库级 join 校验器一次性复核：
`scripts/verify_simulator_external_evidence.py` 会重新计算文件 SHA-256，绑定三种机型的
runtime/AI 场景 manifest 和当前 parity，并检查统一镜像、GPS 质量、truth-free/hardware 边界、
AI target/armed/track 与容器清理。当前 join artifact 为
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260901-pre.8.json`（SHA-256
`43D34D1E7697CDB950816B9C1057104EB4F90820C60AF1A5163B081174BC3632`），`gate_passed=true`。
这是 SIM 文件级一致性子门，不替代桌面 QGC 人工验收、室外 GNSS、真实 Pixhawk/Jetson/相机、
执行器、HIL/REAL 或最终 `v0.3.0` 发布门。

join 后的当前软件验收快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-s3-faults-v10.json`（SHA-256
`A3E2CC98F3B3117D3C36E1FEF15DAA2E9CAB41E4ADB31352FA0397CA74BFEDE5`），核心测试 `2200`、QGC
自定义 `66/66`、脚本退出码 `0`；portable simple-boot 日志
`artifacts/evaluation/qgc-simple-boot-20260901-external-join-v8.log`（SHA-256
`B329814985CF63C04460E2F8DD9872CFCF2A96D48DB96DF051564578287F0F91`）退出码 `0`。这仍是
`v0.3.0-pre.8` 软件证据，现场硬件与人工 Fly View 状态保持未声明。

### S4 SIM 安全证据（当前工作树）

Windows 软件验收现在还会执行一条 deterministic S4 子门，产物固定为
`artifacts/evaluation/simulator-safety-acceptance-latest.json`。该子门复用生产目标选择池、
MAVLink authority/watchdog、`SafetyRuleEngine` 和 Mission 状态机，覆盖 VTOL、固定翼、多旋翼：
`CANCEL_TRK` 清除目标后不残留框，QGC/PX4 非 loopback 或非 allowlist 消息被拒，watchdog 后
进入 `FAILSAFE` 且 AI 不得续控，围栏/GPS/极点坐标 fail-closed，`TERMINATED` 后不接受新事件。

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
python scripts\run_simulator_safety_acceptance.py `
  --out artifacts\evaluation\simulator-safety-acceptance-latest.json
python scripts\verify_simulator_safety_evidence.py `
  artifacts\evaluation\simulator-safety-acceptance-latest.json
```

`run_goal_acceptance.ps1` 会在总快照中写入该文件的 SHA-256、verifier 退出码和六个 check。
对应 Schema 为 [`simulator-safety-evidence.schema.json`](../configs/schemas/simulator-safety-evidence.schema.json)。
这只是软件/SIM fail-closed 证据；硬件、室外 GNSS、真实 Pixhawk/Jetson/相机、HIL/REAL 和
人工 Fly View 仍保持 `not_claimed`/`offline_pending`。

本轮完整总验收快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-s4-safety-v11.json`，SHA-256
`38627B36E6522DFFF76ED7B7BFECC0E2C7F795938C1C45FD48CFD25CBFA0BCDE`；核心测试收集 `2209`、
QGC 自定义回归 `66/66`，脚本退出码 `0`。S4 artifact
`artifacts/evaluation/simulator-safety-acceptance-latest.json` 的 SHA-256 为
`021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`，verifier 退出码为
`0`。现场预检仍为 `hardware_link_ready=false`、`hardware_status=offline_pending`；该快照
保持 `v0.3.0-pre.8`，不提前关闭室外 GNSS、真实设备、HIL/REAL 或人工 Fly View 门。

同一 pre.8 构建随后执行增量 Release/portable 构建，Ninja 返回 `no work to do`，脚本退出码
`0`；portable `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output`
也退出 `0`。启动日志为 `artifacts/evaluation/qgc-simple-boot-20260901-continue.log`
（SHA-256 `8D6A33F8DE158EEED2E9AB526645953C391F2BD4BB584A8F0917012CC5FDA4DB`），Release 与
portable 主 EXE 保持 `46,575,104` bytes、SHA-256
`61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`。

E7 release-gate 的现场绑定也已接通：生成四模式清单时传入
`--real-field-link-preflight <report.json>`，清单会保存报告相对路径、SHA-256、字节数和严格
`verify_field_link_report(..., require_ready=True)` 结果；`check_release_gate.py` 读取时再次
重算并复核。预发布清单省略该字段仍向后兼容，但最终 `release` 通道会因缺少或未通过的
`real_field_link` 保持阻断。

同一生成器也接受 `--simulator-safety-evidence <simulator-safety.json>`（别名
`--sim-safety-evidence`）。它只在 S4 runner/verifier 均通过时写入顶层
`simulator_safety` 记录；`check_release_gate.py` 会重新哈希并复核六个 check。pre-release
可省略该字段以兼容历史清单，最终 `release` 通道则把它作为必需的 SIM 软件安全绑定；这
不会替代现场 GNSS、Pixhawk/Jetson/相机、HIL/REAL 或人工 Fly View 验收。
E7 绑定后的完整 goal acceptance 重跑为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-s5-release-gate-safety-v12.json`
（SHA-256 `C1FED75A38FAD315AE167FA47457F44D7D882E64A50CA1AFE63DBAE79A795DEA`），核心测试
`2215`、QGC 自定义回归 `66/66`、脚本退出码 `0`；软件为
`pass_with_environment_gap`，现场仍为 `offline_pending`。
随后针对坏路径/哈希异常 fail-closed 处理的最终重跑为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-s5-release-gate-safety-v13.json`
（SHA-256 `20C790F384918B8457CC3A2BA54401B20FF55B7786214D82A2CF385864308134`），核心测试
`2215`、QGC 自定义回归 `66/66`、脚本退出码 `0`；S4 仍通过，现场链路继续
`offline_pending`。

绑定最新 `192.168.1.0/24` 只读发现报告后重跑的总验收为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-network-discovery-v14.json`
（SHA-256 `A6CEAEDFA6AE583B118E8E69BF39102E13FB903A0E1BE0542379970C4C952D25`），核心测试
`2215`、QGC 自定义回归 `66/66`、S4 verifier `0`、脚本退出码 `0`。该报告只更新现场
provenance，`hardware_link_ready=false`/`offline_pending` 保持不变。

配套的 E7 pre.8 清单为
`artifacts/evaluation/release-gate-pre.8-current-20260901-network-discovery-s4-bound.json`
（SHA-256 `D4E9412EED3F6CF2EA8E6DB11D1F81AC4A35826B7F343C682C4EFEAF619E631D`），聚合报告
SHA-256 `98BD437EEBEB0383901403B377E3B04C69D4DDDF2EAADDDC2AA97DCDAA52E66A`；S4 通过，
现场链路仍 `offline_pending`，未改变最终发布门。

### 四模式 evidence envelope（pre.8）

四类 runner 的详细 JSON 先用
`scripts/create_release_mode_evidence.py` 封装，再交给 E7 清单生成器。封装器只复制显式的
源 `gate_passed`，并记录源路径、SHA-256、事件和运行模式；它不会从文件名、goal snapshot、
fixture 或状态字段推断通过。源文件缺少 `runtime_mode` 或模式不一致时，必须显式使用
`--allow-missing-source-runtime-mode` / `--allow-source-runtime-mode-mismatch`，且原因会保留在
输出中。机器可读契约是
[`configs/schemas/release-mode-evidence.schema.json`](../configs/schemas/release-mode-evidence.schema.json)。

当前 pre.8 已生成并绑定的诊断清单/报告（旧的无 `-field-rerun` 文件保留为历史 provenance）：

```text
artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun.json
  SHA-256 A13D0223504221C986645F7EE73C6F3B15F8B1656687CE66421FCA8BD80F0A94
artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-report.json
  SHA-256 746FBF1CD8D144D532E67B7AAF0E0E615A03B11397801DC190A88E5767F4AD43
```

为让 replay-set 绑定状态可直接审计，另有补充清单
`artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-replay-bound.json`
（SHA-256 `261CB2755E55013278E9BDEA698AC80D110B55A09F79193C52DD91A4AB27B75C`）及聚合报告
`artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-replay-bound-report.json`
（SHA-256 `DDE5D24A785D07AE1F40D21EBA8D27B379EBD0B5CAE5CBC9633786D50FDA96C8`）。它明确保留
`replay_set.present=true` 但 `passed=false`、现场 `hardware_link_ready=false`，不替换
field-rerun 权威清单，也不代表最终发布门通过。

桌面 exact-client 捕获脚本随后启动并清理了便携 QGC，但 Task Manager 覆盖了 QGC 客户区，
所以脚本按单一原生界面规则停止，没有生成可用 PNG。原始观察报告为
`artifacts/evaluation/qgc-native-style-20260901-continue/process-observation.json`（3423 bytes，
SHA-256 `0EDEB12E5BC121BB7AFE86947BC22A75420A7605C9997C9B21FB4B5D61F7F81E`），
`single_native_surface_machine_check=false`、`operator_visual_acceptance=not_claimed`，并确认
`MultiDetectGCS.exe` 已退出。脚本支持显式 `-HideStubbornOccluders` 兜底，但本机 Task Manager
属于更高完整性窗口，普通 PowerShell 无法临时隐藏，仍需手工最小化/关闭后重跑同一
`capture_qgc_native_style.ps1` 命令；该桌面环境失败不改变 portable 产物或发布门状态。
带 `-HideStubbornOccluders` 的重试仍因同一高完整性 Task Manager fail-closed；对应观察报告为
`artifacts/evaluation/qgc-native-style-20260901-continue-hide-v2/process-observation.json`（3574 bytes，
SHA-256 `4464220068A428BC0DAFCC96CAAE3B60D3C9A1C21A640800FF74627A0521B23B`），其中
`hidden_occluder_count=0`、`single_native_surface_machine_check=false`，且 QGC 已退出。

该报告的 parity 五项均为 `true`，REPLAY/SIM/HIL runner 源 gate 均为真；本轮 SIM envelope
已切换到 900 帧 combined PX4/Gazebo/QGC 成功运行：
`artifacts/evaluation/combined-sim-qgc-20260831T234111Z-53232/combined-evidence.json`，
SHA-256 `5194CC77F45572483B6A17A21FC8FC0DBE222BE62B9A8EB1CC14777C1B464F6C`。
对应 envelope `artifacts/evaluation/release-mode-sim-20260901-pre.8-rerun.json` 的 SHA-256 为
`9862E26984487822DF9DCCB1D0673889C28ACD50DE1DFB02385AC829E11D516F`。
该运行的相机/CameraInfo/NavSat readiness、VTOL、PX4 Mission、原生 QGC QQuickItem 抓帧和
清理机器门均通过，但人工 Fly View、REAL/HIL 硬件、real-control、Mission 状态和现场链路
尚未闭合，故 `gate_passed=false`、`release_allowed=false`。portable 构建仍只使用
`v0.3.0-pre.8`；现场恢复后要从新的 runner 文件重新封装、重算清单和 SHA-256。

本轮重新执行的 pre.8 goal acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-real-field-link-gate.json`
（SHA-256 `00F4A58B2CC809665F1580CAB5E143251387ABE671EC7D6BBC793240C97610E2`）；核心测试收集
`2140`、QGC 自定义回归 `66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`。
该快照绑定的三采样 watch 仍为 `hardware_link_ready=false`/verifier exit `1`，所以没有升级
Jetson、GNSS、Pixhawk、执行器、HIL/REAL 或人工 Fly View 门。

2026-09-01 随后又做了一次最新只读现场预检并刷新总验收。现场报告为
`artifacts/evaluation/field-link-preflight-20260901-continue-latest.json`
（2351 bytes，SHA-256 `67FAD84F26C1DDC0301CD0197284AC70FE93281BC7773A9597093B58EFF68AC2`），三个现场端点
仍超时而本机 `14580/14581` 可绑定，`hardware_link_ready=false`、`--require-all` 退出码为 `1`。
绑定该报告的当前清单/报告为
`artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun.json`
（SHA-256 `A13D0223504221C986645F7EE73C6F3B15F8B1656687CE66421FCA8BD80F0A94`）和
`artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-report.json`
（SHA-256 `746FBF1CD8D144D532E67B7AAF0E0E615A03B11397801DC190A88E5767F4AD43`）。
该刷新仅更新现场 provenance，不改变任何真实设备或最终发布门状态。

现场复核随后扩展为显式 `192.168.1.0/24` 的只读服务发现：
`artifacts/evaluation/field-link-discovery-20260901-continue-subnet.json`（SHA-256
`89F80BD5BD1276D28378484481ACBAF3F8C1865FB12D8605903250FF2D71EBC7`）扫描 `254` 个地址，
仅识别到三个 SSH banner，MAVLink/RTSP 均无命中。基于当前 parity 重建的四模式清单为
`artifacts/evaluation/release-gate-pre.8-current-20260901-subnet-discovery.json`（SHA-256
`93C15C0BE48EF4EAEB1ED259A62321BC4F69998B5B13CD61801594812ABC77EB`），报告为
`artifacts/evaluation/release-gate-pre.8-current-20260901-subnet-discovery-report.json`
（SHA-256 `0416E67F56EE143FC903446E8D18DCEDC2925D0EECE810F939321D1BC5A41BF5`）。清单的
五组 parity 一致，但现场链路和其它真实发布门仍 fail-closed；发现结果不会自动选择
Jetson/GR01/相机端点。

随后修复组合验收脚本中预检文档被后续初始化覆盖的问题，并重新执行总验收：
`artifacts/evaluation/multidetect-goal-acceptance-20260901-field-refresh-20260901-054426.json`
（SHA-256 `0B1A8F418281C7AC04B32EAB857E0D34B57C80B4F7B976D56F7DCA5D9DB2DA14`），核心测试
`2152`、QGC 自定义回归 `66/66`，脚本退出码 `0`。修复后成功路径写入的
`combined-evidence.json` 会保留预检 `readiness` 与 `docker_diagnostics`，软件仍为
`pass_with_environment_gap`、硬件仍为 `offline_pending`。

Docker Desktop 进程停止后，SIM 外部运行时的最新只读复探为
`artifacts/evaluation/simulator-runtime-preflight-20260901-continue-20260901-054222.json`
（文件 SHA-256 `F7BA35ACA79E39A225F4CA65841A253E323745244E860EAA661E2BE209DFB5F8`，canonical
digest `351faffb851922ed0643af310727b2135cf192834e2a6771d26c21a4d7635ae4`）。Docker client
仍可定位但 server named pipe 返回“找不到文件”，PX4/Gazebo 仍不在 PATH，
`ready_to_attempt=false`；该证据不改变 portable EXE 或任何 SIM/HIL/REAL/硬件门状态。

### 台式机 external SIM/AI 与当前 portable 复核（2026-09-01）

Docker Desktop 恢复后，固定 digest 的 PX4/Gazebo 外部 runtime 与 live AI `TRACK` 已重新通过：
runtime evidence 为
`artifacts/evaluation/simulator-docker-acceptance-20260901-desktop.json`
（SHA-256 `C4D191D46604BF86D1BD2BBA64F3907E2C35AB3F1F3369BEFFFCCA709305A045`），AI evidence 为
`artifacts/evaluation/simulator-docker-ai-acceptance-real-20260901-desktop.json`
（SHA-256 `B53ECA7F8D8F0C4D830C656FA22D3C1C39BD6DAA1E0C8D5719CDE3A6C68733B7`）；两者均为
pre.8、truth-free 合成 perception，硬件/人工视觉保持 `not_claimed`。三机型 join 为
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260901-desktop-v2.json`
（SHA-256 `C2FFB02152A96026B639CCA7EE50C243B22BEC94A5FF41A0421A6A409DEA2382`，
`gate_passed=true`）。随后当前 portable 增量构建与 `--simple-boot-test` 均退出 `0`，主 EXE
SHA-256 为 `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`；完整 goal
acceptance 为 `artifacts/evaluation/multidetect-goal-acceptance-20260901-desktop-v11.json`
（SHA-256 `2323A780DA7D62EC3AE267A4871B2D51FB7EBDF757294783FD300336D658158C`）。
这些刷新只关闭 software/SIM 子门，不改变现场 Jetson/相机/室外 GNSS、真实 Pixhawk/执行器、
HIL/REAL、人工 Fly View 或最终 `v0.3.0` 发布条件。

随后生成并绑定现场预检的最新总验收为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-desktop-v12.json`（23,195 bytes，
SHA-256 `D57149544BFA4CE614F42B8751E93CA5156DFE328D279EF48F793B994FC91F6A`）。其中
`field_link_preflight.provided=true`、`verified=true`，但
`hardware_link_ready=false`、`gate_passed=false`、verifier 退出码为 `1`；核心测试
`2319`、QGC custom `66/66` 和软件状态 `pass` 保持不变。

紧接着的有界只读现场预检为
`artifacts/evaluation/field-link-preflight-20260901-desktop-rerun.json`（4,982 bytes，
SHA-256 `0A9C8561E2E91886A1555CE6F2BCE4AF2E1F720DCEC4AC969FFD5561F4A9586B`）：Jetson
`192.168.144.20:22`、GR01 `192.168.144.11:5760`、相机 `192.168.144.108:554` 均超时，
本机 `14580/14581` 可绑定；`samples_requested=3`、`samples_collected=1`，首个失败即停，
`hardware_link_ready=false`。该结果只记录当前网络事实，不修改路由或发送控制。

| 版本 | 主运行入口 | 状态 |
| --- | --- | --- |
| `v0.3.0-pre.8` | `build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe` | **E3 BUILT + SIM E4 combined machine gate + native-QGC machine smoke**：原生 QGC Fly View toolbar drawer 已加入只读 `GPS/GNSS` 与 `RANGE_STATUS` 质量行，显示门原因、source age/data age；GPS/目标事件回放新鲜度、WGS-84 落图、map 生命周期保护与证据快照 fail-closed、三机型 Mission dispatcher、Release/portable、RTSP helper、C++ 自检、portable 依赖闭包和 900-frame Combined SIM Mission gate 均通过；当前主 EXE `46,575,616` bytes，SHA-256 `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`；最新 goal acceptance 为 `multidetect-goal-acceptance-20260901-combined-qgc-v5.json`，收集 2315 项核心测试、QGC custom `66/66`，软件门为 `pass_with_environment_gap`、硬件为 `offline_pending`；最新现场预检 `field-link-preflight-20260901-final-continue.json` 仍显示 Jetson/GR01/相机端点超时；现场协议 watch、人工 Fly View、桌面 GPU 视频、HIL/REAL/室外 GNSS 仍 pending |
| `v0.3.0-pre.7` | `build/windows-v0.3.0-pre.7-release/portable-v0.3.0-pre.7/bin/MultiDetectGCS.exe` | **E3 BUILT + SIM E4 machine + native-QGC machine smoke**：Release/portable、RTSP helper、三个 C++ 自检、portable 依赖闭包、600-frame combined PX4/Gazebo/QGC VTOL Mission gate 与 exact-client 原生 QGC 截图已完成；主 EXE `46,551,552` bytes，SHA-256 `B24754D7751521632C3C1250D69B4476CD13B8A6DCA09248A79B26CCD93973EA`；人工 Fly View、HIL/REAL/室外 GNSS 仍 pending |
| `v0.3.0-pre.6` | `build/windows-v0.3.0-pre.6-release/portable-v0.3.0-pre.6/bin/MultiDetectGCS.exe` | **E3 BUILT**：Release/portable、helper、三个 C++ self-test 与 portable inventory 已完成；主 EXE `46,551,552` bytes，当前重建 SHA-256 `AAFA5448E74C9D5041360F4384C98E140C5197A856751768DD88135436F4C662`；HIL/REAL/室外 GNSS 仍 pending |
| `v0.3.0-pre.4` | `build/windows-v0.3.0-pre.4-release/portable-v0.3.0-pre.4/bin/MultiDetectGCS.exe` | **E3 BUILT + SIM E4 machine（2026-08-26）**：Release/portable、helper、自检、600-frame combined VTOL Mission/QGC gate 均已封存；人工 Fly View、HIL/REAL/室外 GNSS 仍 pending |
| `v0.3.0-pre.3` | `build/windows-v0.3.0-pre.3-release/portable-v0.3.0-pre.3/bin/MultiDetectGCS.exe` | **E4 INTEGRATED（2026-08-25）**：pre.3 E3 构建、自检、portable 闭包、真实 QGC RTSP/metadata barrier 和原生 Fly View 画面均有独立证据 |
| `v0.3.0-pre.2` | `build/windows-v0.3.0-pre.2-release/portable-v0.3.0-pre.2/bin/MultiDetectGCS.exe` | **E3 BUILT（2026-08-25）**：2623-step 构建图、两个 C++ 自检、install/windeployqt、portable 闭包和 15 秒启动冒烟通过；wrapper 退出码 `0`，大小和 SHA-256 见下文 |
| `v0.3.0-pre.1` | `build/windows-v0.3.0-pre.1-release/portable-v0.3.0-pre.1/bin/MultiDetectGCS.exe` | **E3 BUILT（2026-08-25）**：构建/部署退出码 `0`，两个 C++ 自检通过，完整 portable 依赖闭包与 15 秒启动冒烟通过；大小和 SHA-256 见下文 |
| `v0.2.0` | `build/windows-v0.2.0-release/portable-v0.2.0/bin/MultiDetectGCS.exe` | 已验证回退基线；Release 与 portable 已在台式机验证，NSIS 未启用 |

### Target geolocation pole-boundary gate v31（2026-09-01）

目标地理位置的 WGS-84 坐标域在 Python 和 QGC native wire 层统一为：纬度严格 `(-90, 90)`，
经度允许 `[-180, 180]`。精确南/北极点没有稳定的局部经度方向，已在
`TargetGeolocation`、消息模型、QGC C++ 解码器、地图落点、复制、快照保存与 JSONL 重载路径
fail-closed；已签名的极点变异帧也会在协议层拒绝，不会留下不可投影的目标点。
对应完整验收证据为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-target-geo-pole-v31.json`
（SHA-256 `CC6371291F416E62E1308C78CF70CC2E08B10854D7B5A5817194E5B5164D18EE`），脚本退出码 `0`，
核心测试收集 `2101` 项，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、
hardware=`offline_pending`。canonical parity 文件和同字节审计快照为
`configs/release-parity.json` / `artifacts/evaluation/release-parity-pre.8-20260901-target-geo-pole.json`，
SHA-256 `54BB83A9E7091259C38BE9A17B4996BF7B9B810BF69DA76D45F29315A9993A62`。Release/portable 主 EXE
仍为 `46,575,104` bytes，SHA-256 `61F16B6A13247316E356C7B626E79BB1033A6AC03C54D4DD676F75BCCD4CE497`；
Operator、44-fragment depth-grid 和 RTSP wire 三个 C++ 自检均退出 `0`。
同一 portable 目录的 `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output`
冒烟退出码为 `0`，日志 `artifacts/evaluation/qgc-simple-boot-20260901-target-geo-pole.log`
的 SHA-256 为 `E2BF6D1E4317AA721984583578D944B0D7285A8754F7D62C2BC67F91AB73CAB2`；未出现
depth-grid bind failure、QML 初始化失败、断言或崩溃。

同日现场链路 watch 证据为
`artifacts/evaluation/field-link-preflight-20260901-followup.json`
（SHA-256 `1873247B49F30856483F22635CC28620458990BC78CA2C2A28BE2BD25355549E`）。
使用三次、间隔 1 秒、首个失败即停止的只读协议预检；Jetson SSH、GR01 MAVLink 和相机 RTSP
均超时，本机 `14580/14581` 可绑定，`samples_collected=1/3`、
`hardware_link_ready=false`、`--require-all` 退出码 `1`。这是现场连接失败证据，不能替代
GNSS fix、真实 Pixhawk/执行器、HIL/REAL 或人工 Fly View 验收；接通飞机 Ethernet 并确认
机载 IP 后必须用同一命令重跑。

交接前可用 `scripts/verify_field_link_preflight.py <report.json>` 单独重算报告 SHA-256、字节数、
watch 样本摘要和 `hardware_link_ready`；默认失败报告返回 `1`，`--allow-unready` 只用于查看诊断报告，
不会放宽后续 `--require-link-preflight` 的现场会话门。安装后的等价入口为
`multidetect-field-preflight-verify`。

本机外部 3-D 运行时也有独立的只读预检证据
`artifacts/evaluation/simulator-runtime-preflight-20260901-pre.8.json`（文件 SHA-256
`E973A4F3B93B255F61D8F600E4D90F8EE251D9D106F010D83A12BB93538506C3`）。native PX4/Gazebo
命令未发现，Docker 客户端可定位但 daemon 探针超时，故 `ready_to_attempt=false`、
`execution_mode=unavailable`；该状态不改变 portable EXE 构建结果，也不等价于 SIM/HIL/REAL。

### Target geolocation source-age gate v30（2026-08-31）

`source_captured_at_s` 到目标地理位置产出时间的年龄现在由生产者、Python 消息模型、wire
编码器/解码器和 QGC C++ 解码器共同限制为 `<=1.5 s`。合格但过期的输入会原子发布为
`available=false` 或在解码时拒绝，因而不会在地图、复制、快照或重放链路中留下旧坐标。
对应完整验收证据为
`artifacts/evaluation/multidetect-goal-acceptance-20260831-target-geo-age-v30.json`
（SHA-256 `772672FE9FEAAD9369DC8B31E1A6F5B30F1EB2C1A0279ED53A3C51F2A7FD95D1`），脚本退出码 `0`，
核心测试收集 `2097` 项，QGC 自定义测试 `66/66`，software=`pass_with_environment_gap`、
hardware=`offline_pending`。canonical parity 文件已由生成器同步（SHA-256
`676817B29C868FCEF90606BFD9E8FE0A2A6DD69E96177E6AE7B7AE0506A86964`）。

### Dispatcher v24 软件验收（2026-08-31）

三机型 PX4 Mission dispatcher 与 `x500` 别名归一化已通过独立回归：
`artifacts/evaluation/multirotor-mission-compile-acceptance-20260831-dispatcher.json`
（SHA-256 `ADF8BAB8D4887697A1CF09E54AB8C21CEBDD9ACA0312B0A89514FA613F14DAB2`）。
本轮 goal acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260831-dispatcher-v24.json`
（SHA-256 `15CC5F31CF4EEBD7500ED63DD093F96C5108AF5D2C980F4087B5CC0463B8C4D5`），核心
pytest 收集 `2083` 项，QGC 自定义回归 `66/66`，软件门为
`pass_with_environment_gap`、硬件门为 `offline_pending`。portable EXE 指纹仍为
`F847DF0503744BA7B5C2E9446330DB3A39A5BA8CBEA7BB17BA542A133EB7725C`；Jetson、相机、室外
GNSS、真实 Pixhawk/执行器、HIL/REAL 和人工 Fly View 仍未完成，版本继续保持 `v0.3.0-pre.8`。

portable 原生离线启动冒烟使用同一 EXE，退出码为 `0`；日志为
`artifacts/evaluation/qgc-simple-boot-20260831-dispatcher.log`（SHA-256
`39D9D3F102ABD986A0C1EB71880C8C5FF5C40CADA04A0F75C4B845388BEAB3AF`）。

## 运行与校验

成功完成 portable 部署后，在 PowerShell 中校验主 EXE：

```powershell
$exe = '.\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Portable EXE 尚未生成: $exe"
}
Get-Item -LiteralPath $exe | Select-Object FullName, Length, LastWriteTime
Get-FileHash -LiteralPath $exe -Algorithm SHA256
```

只有文件真实存在、依赖闭包检查通过且输出与对应发布记录完全一致时才运行。pre.8 本轮已封存
对应制品哈希；pre.4 的历史制品仍作为回退基线保留。启动时把 portable
的 `bin` 目录作为工作目录。当前默认目标不是 NSIS 安装包；不要把裸
`Release\MultiDetectGCS.exe` 单独复制出去。operator key、MAVLink signing key、RTSP 凭据和
Jetson 地址属于部署配置，不能提交到仓库。

### v0.3.0-pre.8 Windows portable 制品（2026-09-03 当前构建）

| 制品 | 大小 | SHA-256 |
|---|---:|---|
| `build/windows-v0.3.0-pre.8-release/Release/MultiDetectGCS.exe` | `46,575,616` bytes | `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40` |
| `build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe` | `46,575,616` bytes | `4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40` |
| `build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectReplayRtspPublisher.exe` | `95,744` bytes | `DE94D5404BEF48B80919BFC06465BD53EBFD50A8B65A50F38DA3BCDDADB58312` |

便携 helper 的当前 RTSP 多客户端回归证据为
`artifacts/evaluation/replay-rtsp-multiclient-20260903-pre.8-v9-portable-4clients-shared.json`
（3,299 bytes，SHA-256
`994818A9FE96AD5D1BF1C02286E8A1FF67181F63F203C89B7FB6CE75076308B2`）。四个并发 raw
RTSP/TCP 客户端均完成 `OPTIONS → DESCRIBE → SETUP → PLAY`，检查 H.264/正帧率 SDP、
interleaved RTPv2/PT96 包增量、两个稳态 fan-out frame、EOS 和 helper summary，记录
`gate_passed=true`。当 `--minimum-client-count > 1` 时 helper 使用 shared media fan-out；
默认单客户端仍使用非共享 generation 路径。该记录仍是 software-only local transport 证据，
不能替代真实相机/Jetson、室外 GNSS、Pixhawk、HIL/REAL 或人工 Fly View 验收。此前双客户端
`v4-portable` 文件保留为历史证据。

portable 目录共 `1,015` 个文件、`529,304,901` bytes，其中 `bin/` 为 `233` 个文件；Release 与
portable 主 EXE 字节完全一致。构建 wrapper 退出码为 `0`。exact-client 原生 QGC machine
capture 为 `build/qgc-native-style-pre.8-cu-authorized-20260826/qgc-native-style.png`，`475,726` bytes，
SHA-256 `32C6461104D01200DF89F35B45C7CE87E58814E0E3273BA2B8596AB62CDD1263`；
`single_native_surface_machine_check=true`，但 `operator_visual_acceptance=not_claimed`、
`qml_rendering_proved=false`，不替代人工 Computer Use/Fly View 验收。

证据卡片空值格式化修复后的 portable 主 EXE 已用
`ground-control/MultiDetectGCS/tools/run_tests.py --verify-only --headless --timeout 45`
完成启动冒烟，退出码为 `0`；证据卡片修复阶段的 goal acceptance 历史快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-rebuilt-evidence-card.json`，
SHA-256 为 `1EA3C005B02B8E7022C33BB4446D2E7C21585495BB66C8AACF7F5244E2AFA881`，其中
`qgc_exe.sha256` 与上述 `600E98F31EFD8D9751F954EDA0473D0746543F1504245979A96EBB1F856D19DE`
一致；该历史快照保留，不再代表当前 `latest`。

共享交互 `ManualClock`/`RuntimeDescriptor` 回归接入后的 goal acceptance 历史快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-interactive-clock.json`，
SHA-256 为 `5AC0A405BF6B32405903FFFB8E58D37DAF45F817979A5E5275762854FBDB363B`，并同步为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（随后已由 GPS/回放快照取代）；该快照收集核心测试 `1861` 项、
QGC 自定义测试 `63/63`，软件状态 `pass_with_environment_gap`、硬件状态 `offline_pending`，
且 `qgc_exe.sha256` 与当时的 portable EXE 一致；当前制品指纹见上方表格。

GPS/目标事件回放与 QGC 落图修复后的首个 goal acceptance 快照（历史记录）为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-gps-replay.json`，SHA-256 为
`5CB38AA3654130F5025B2227802F9FAE974CD8EC73A1A261460B4E5B14479171`。加入 E7 清单生成器与
回归后，再收紧 map/PiP 生命周期与本地证据 JSONL 验证；该 `final-regression` 文件作为历史快照
保留。传感器 transport teardown 修复后的 QA 快照（历史记录）为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa.json`，SHA-256
`440AB514681408DB5B583C0AD9A62054441004BD107756C0229423546AF36334`。随后加入 release-gate
runtime/event 归一化回归后的 QA3 历史快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa3.json`，SHA-256
`ED550A122814A32D8823CC482A909B1BEF4AC0D234E03D242185E33BC37EA1F3`（历史快照）。加入
canonical parity 同步回归后的 QA4 历史快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa4.json`，SHA-256
`C9A04BA94DDD159BBBC979735BCD53910335A612B256EA0E1195D512D404D552`，并同步为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本）。网关 graceful-drain、
GPS 原始质量二次校验和物理网卡选择修复后的 QA5/QA6 快照作为历史记录保留。Windows
PowerShell 5.1 无参数入口与 UTF-8 无 BOM 证据写入修复后的 QA7 历史快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa7.json`，SHA-256
`D82F07B6E2935D40D0A16CF87644EB7AD0AD91FE9A67A3620B29CC78583AAAFF`，完整脚本退出码 `0`，核心测试
`1937 passed / 1 skipped`（共收集 `1938` 项；唯一跳过项是该 Windows 账户不支持符号链接）、QGC 自定义测试 `63/63`；回放绑定 GPS observation timestamp/age/freshness，且发布坐标前重新验证
fix、卫星数、水平/垂直精度和时间新鲜度；过期或未来样本 fail-closed，QGC 1σ 圆使用 WGS-84
局部曲率与日期变更线归一化，无效坐标/距离/方位、过期 source age 或损坏 JSONL 记录不会被还原成
假目标。该证据仍为
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，不替代真实
Pixhawk/Jetson/相机/室外 GNSS、HIL/REAL、实飞或人工 Fly View 验收。

同日的 disposable Docker/PX4/Gazebo AI armed-heartbeat 复验另存为
`artifacts/evaluation/simulator-docker-ai-acceptance-20260830-ack-gps.json`，SHA-256
`25947F39FF2FA66C02FBD0F2C4CB02C0CA2586701C6FA200B9AD148730A3D279`，容器日志 SHA-256
`E848D1347A8064C92B39532F1F05BC9938CB8B50FC96BA31E5C0159E0B046220`。它明确记录
`minimum_gps_fix_type=3`、PX4 autopilot component `1`、`armed_observed=true`、
`ai_steps=1`、`gate_passed=true` 以及 5 个 `COMMAND_ACK` 的身份筛选结果；这是
disposable SITL machine evidence，不替代真实 Pixhawk/Jetson/室外 GNSS、执行器、HIL/REAL
或人工 Fly View 验收。

同日更新后的 Docker/PX4/Gazebo armed-heartbeat 证据（QA7 runtime）为
`artifacts/evaluation/simulator-docker-ai-acceptance-20260830T123242Z-qa7.json`，SHA-256
`13760F68761D2774A8F3ABF8E4D379E09AA5D8FD4171552001AF25AFE00F9563`；结果 sidecar
`simulator-docker-ai-acceptance-20260830T123242Z-qa7.result.json` 的 SHA-256 为
`7A09BC0224DC003A01DF0DA607910D8BA83788740475C79CDF9D216241666351`，容器日志 SHA-256 为
`C6BCBDE08D1430C83158966FA518DDF8E14DCB3A4EE8F989D2B823EF7940E8B3`。该 run 的
`gate_passed=true`、`ai_policy_gate_passed=true`、`ai_steps=1`、`armed_observed=true`，但仍是
Docker/SITL machine evidence；真实 Pixhawk/Jetson/相机、室外 GNSS、HIL/REAL 和实飞保持
`not_claimed`。

QGC source-age/frame-hold、SIM world-origin 和 QGC 自定义测试更新后的 QA8 快照现作为历史记录保留：
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa8.json`，SHA-256
`A9C1AC9CE94B19BDE4AB551C27EEC4780554D601C913BE157CD86AE431C5ED98`；脚本退出码 `0`，核心测试
收集 `1943` 项、QGC 自定义回归 `64/64`，并绑定当时的 `46,571,008` bytes portable EXE。
QA8 仍为 `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，不提升
真实 Pixhawk/Jetson/相机、室外 GNSS、HIL/REAL、实飞或人工 Fly View 状态。

### QA10 goal acceptance（2026-08-30 历史快照）

QA10 原文件和 SHA 保留为历史记录：
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa10.json`，SHA-256
`1F3515C50D2EA95D481450189ED72AC2FCBD814B965F342C5777BED6D30B6F65`。

### QA11 goal acceptance（2026-08-30 历史快照）

QA11 原文件和 SHA 保留为历史记录：
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa11.json`，SHA-256
`FFB41D20E2C220C8391D82DD062AF44C39DED020DBD2CD12C3B702A169403993`；脚本退出码 `0`，并已提升为
曾经提升为 `artifacts/evaluation/multidetect-goal-acceptance-latest.json`（随后由 QA22 取代）。核心 pytest 收集
`1964` 项，`1963 passed / 1 skipped`（Windows 账户不支持符号链接），QGC 自定义回归 `64/64`；
QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和
portable headless smoke 均通过。当前 Release/portable 主 EXE 均为 `46,574,080` bytes，SHA-256
`E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。
QA11 的 `software_status=pass_with_environment_gap`、`hardware_status=offline_pending`；Ethernet 2、
Jetson/相机、真实 Pixhawk/室外 GNSS、执行器、HIL/REAL、实飞和人工 Fly View 仍是 live gates，最终
`v0.3.0` 未发布。QA11 JSON 在验收启动时记录源码 provenance：`HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、
`dirty_entry_count=290`、`worktree_clean=false`、`status_sha256=5e156cf0952025104665e287d9a87099acb2e989b8c0b9d67719c19befc86a1e`；
它仍是 dirty snapshot 证据，不是干净提交发布制品。

### QA13 goal acceptance（2026-08-30 历史快照）

QA13 原始 `v0.3.0-pre.8` goal acceptance 保留为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa13.json`，SHA-256
`EF1684856B274607A2C39F951D620803149495C9F323863CC096788528E16069`；脚本退出码 `0`。QA13 当时核心 pytest 收集
`1973` 项，`1972 passed / 1 skipped`，QGC 自定义回归 `64/64`；QGC protocol self-test、QML parse、
Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。该快照的
Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
`E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA13 的
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，以及
`operator_visual_acceptance=not_claimed` 仍保持；最终 `v0.3.0` 未发布。源码 provenance 为
`HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、`worktree_clean=false`、
`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。

### QA14 goal acceptance（2026-08-30 历史快照）

QA14 原始 `v0.3.0-pre.8` goal acceptance 保留为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa14.json`，SHA-256
`6856130808EDEB05C543DE24A42F2C17545725C5D6615338ECF52613B5CA3277`；脚本退出码 `0`。QA14 当时核心 pytest 收集
`1975` 项，`1974 passed / 1 skipped`（Windows 账户不支持符号链接），QGC 自定义回归 `64/64`；
QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和
portable headless smoke 均通过。当时 Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
`E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA14 的
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 与
`operator_visual_acceptance=not_claimed` 保持不变；最终 `v0.3.0` 未发布。源码 provenance 为
`HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、`worktree_clean=false`、
`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。

### QA15 goal acceptance（2026-08-30 历史快照）

QA15 原始 `v0.3.0-pre.8` goal acceptance 保留为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa15.json`，SHA-256
`B978D244A7440B3C102E1BDD8408110F511BE2C0209C4BC7DDB340EE56487808`；脚本退出码 `0`，曾提升为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。核心 pytest 收集
`1975` 项，`1974 passed / 1 skipped`（Windows 账户不支持符号链接），QGC 自定义回归 `64/64`；
QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和
portable headless smoke 均通过。当时 Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
`E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA15 的
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 与
`operator_visual_acceptance=not_claimed` 保持不变；最终 `v0.3.0` 未发布。源码 provenance 为
`HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、`worktree_clean=false`、
`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。本轮加入 raw GPS
缺失 `alt`/降级 fix 的原子失效回归，并在失效时恢复先前独立 EKF/LOCAL 高度。

### QA16 goal acceptance（2026-08-30 历史快照）

QA16 原始 `v0.3.0-pre.8` goal acceptance 保留为
`artifacts/evaluation/multidetect-goal-acceptance-20260830-final-qa16.json`，SHA-256
`403B319255A9C2446012BD060C85BB2633DB9A4D7DE45CF4B9BB495D96B234EE`；脚本退出码 `0`，曾提升为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。核心 pytest 收集
`1976` 项，`1975 passed / 1 skipped`（Windows 账户不支持符号链接），QGC 自定义回归 `64/64`；
QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和
portable headless smoke 均通过。当时 Release/portable 主 EXE 为 `46,574,080` bytes，SHA-256
`E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA16 的
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 与
`operator_visual_acceptance=not_claimed` 保持不变；开发线继续是 `v0.3.0-pre.8`，最终 `v0.3.0` 未发布。
源码 provenance 为 `HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、
`worktree_clean=false`、`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
QA16 复验并保留 GPS_RAW_INT 缺失 `alt`/降级 fix/非法坐标/未知卫星哨兵/未来时间戳的原子
fail-closed 语义，并在失效时恢复先前独立 EKF/LOCAL 高度；SIM `arm_on_start`、Docker 标准路径回退
和 canonical parity 继续有效。该历史快照只证明软件/协议门，不升级为真实 Pixhawk、Jetson、室外 GNSS、
HIL/REAL、实飞或人工 Fly View 结论。

### QA17 goal acceptance（2026-08-31 历史快照）

QA17 原始 `v0.3.0-pre.8` goal acceptance 保留为
`artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa17.json`，SHA-256
`BF93CE4FFD7DEE0CA9D166614DAF8D6216BF0ACC7D09808DDF913B3E11D6FFA2`；脚本退出码 `0`，曾提升为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。核心 pytest 收集
`2000` 项，运行输出为 `1999 passed / 1 skipped`（唯一跳过项仍是该 Windows 账户不支持符号链接），QGC 自定义回归
`64/64`；QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware diff check、三个 C++
self-test 和 portable headless smoke 均通过。当时 Release/portable 主 EXE 仍为 `46,574,080` bytes，
SHA-256 `E36FA105E392A7A821D4FA375EE1B1A5A7CF97D2B272874CBFB3D9825DD55150`。QA17 的
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 与
`operator_visual_acceptance=not_claimed` 保持不变；最终 `v0.3.0` 未发布。源码 provenance 为
`HEAD=7f6e6aeb7c00902fb8084bd6e96a0250df685e67`、`dirty_entry_count=293`、`worktree_clean=false`、
`status_sha256=b98a96dd5850dac360714ff836f0818da09338cc382a4bbe3af1f01c3235dbba`。
QA17 已验证 GPI/GPS 原子解析与非法 sentinel 回归：无效坐标、未知卫星、缺失高度、降级 fix 或未来时间戳
不会复用旧 raw 新鲜度/坐标，失效时恢复独立 EKF/LOCAL 高度；该历史快照只证明软件/协议门，不升级为真实
Pixhawk、Jetson、相机、室外 GNSS、HIL/REAL、实飞或人工 Fly View 结论。

### QA18 goal acceptance（2026-08-31 历史快照）

QA18 原始 `v0.3.0-pre.8` goal acceptance 保留为
`artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa18.json`，SHA-256
`230A2EC5EC44982DE4563AEAEB949E32ACD85123D706C320E056EA7BF3951F7F`；脚本退出码 `0`，曾提升为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA18 核心 pytest 收集
`2000` 项，运行输出为 `1999 passed / 1 skipped`（唯一跳过项是该 Windows 账户不支持符号链接）；这些数量
来自当次全量运行输出统计，不是 QA18 JSON 的额外字段。QGC 自定义回归 `64/64`；QGC protocol self-test、QML parse、
Ruff、compileall、CRLF-aware diff check、三个 C++ self-test 和 portable headless smoke 均通过。
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

### QA19 goal acceptance（2026-08-31 历史快照）

当时 `v0.3.0-pre.8` goal acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa19.json`，SHA-256
`082092AB58848D498050901C5BD9768AABF2357569C91D72D531DD105E527515`；脚本退出码 `0`，并已提升为
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

### QA20 goal acceptance（2026-08-31 历史快照）

当前 `v0.3.0-pre.8` goal acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa20.json`，SHA-256
`624B31E03C04D60CD0FF26386C23DC3E6FED19E011375C31DE8578E897ACE756`；脚本退出码 `0`，并已提升为
`artifacts/evaluation/multidetect-goal-acceptance-latest.json`（当时版本；现由 QA22 指向）。QA20 核心 pytest 收集
`2001` 项；QGC 自定义回归 `64/64`；QGC protocol self-test、QML parse、Ruff、compileall、CRLF-aware
`git diff --check`、三个 C++ self-test 和 portable headless smoke 均通过。当前 Release/portable 主 EXE 仍为
`46,574,080` bytes，SHA-256
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

### QA23 range-freshness rebuild (2026-08-31, superseded by QA24)

Canonical portable output was refreshed from the current Release link result. The Release and portable EXE are byte-identical: `46,575,104` bytes, SHA-256 `759C8381C5ED34D791C8A6E9966FD3231196B1727AE1C2DD1DA946F77275E9C3`.

Goal acceptance evidence: `artifacts/evaluation/multidetect-goal-acceptance-20260831-range-freshness-qa3.json` (SHA-256 `4CCE5185EA57EE41E8EE8486ECE535B4294FF6BCA20CC82961CE7B435DB1F295`), script exit `0`, core collection `2022`, QGC custom `66/66`; software=`pass_with_environment_gap`, hardware=`offline_pending`, operator visual=`not_claimed`. This remains prerelease software evidence and does not close REAL, outdoor GNSS, Jetson/camera, HIL, actuator, or final `v0.3.0` gates.

### QA24 depth-grid offline-start rebuild (2026-08-31)

The current pre.8 portable tree was rebuilt after making depth-grid receiver creation conditional on a fully configured signed operator protocol. Offline QGC startup therefore skips the receiver when no HMAC key is present, while configured deployments retain the port-range, collision, and signing checks. The Release and portable EXE are byte-identical: `46,575,104` bytes, SHA-256 `F847DF0503744BA7B5C2E9446330DB3A39A5BA8CBEA7BB17BA542A133EB7725C`.

The new portable `--offline --simple-boot-test --allow-multiple --no-windows-assert-ui --log-output` run exited `0`; log evidence is `artifacts/evaluation/qgc-simple-boot-20260831-depth-grid-fix.log` (SHA-256 `AED8F43CD039490BA5B775C3A3FE4A7DBAC957FE228B5BEC6FE7A20B5E6AD80F`) and contains no depth-grid bind failure. The Release self-test trio also passed: the Python-generated 44-fragment depth-grid fixture round-tripped through `MultiDetectDepthGridProtocolSelfTest`, `MultiDetectOperatorProtocolSelfTest`, and `MultiDetectReplayRtspWireSelfTest` all exited `0`. The latest goal acceptance is `artifacts/evaluation/multidetect-goal-acceptance-20260831-field-capture-verify-v14.json` (SHA-256 `68C9F93C90D3790F3AA8D2F9F11EC8715795F6A0C6CD39A46E091207C264F7EA`), script exit `0`, core collection `2055`, QGC custom `66/66`, software=`pass_with_environment_gap`, hardware=`offline_pending`; it includes the read-only field-session capture and post-capture provenance verifier regression. This is still prerelease software evidence; Jetson/camera/outdoor GNSS, actuator, HIL/REAL, and manual Fly View gates remain open.

### Field-link watch v28 (2026-08-31)

现场链路预检新增 `--samples/--interval-s/--stop-on-failure` 稳定性 watch，仍只做 TCP、SSH banner、
被动 MAVLink 帧、RTSP `OPTIONS` 和本机 UDP bind 检查。最新三次 watch 证据为
`artifacts/evaluation/field-link-watch-20260831-continuation.json`（SHA-256
`17743B74CC2A1A276BDD4C0BAB4B0026F8A6D4C1F834BBDB00E5418EE4F63167`）：三次样本均采集，
但 Jetson/GR01/RTSP 连续超时，`max_consecutive_failures=3`、`stability_ratio=0.0`、
`hardware_link_ready=false`。对应软件门快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-watch-v29.json`（SHA-256
`DEF51B47D75987FE948AB6D2639128B2FBC99E68F11591CABF28542C24D9E96D`），核心测试 `2094`、
QGC 自定义回归 `66/66`，脚本退出码 `0`；portable EXE 指纹未改变。该 watch 证据只表明当前台式机
未接入飞机网络，不关闭 Jetson/相机/室外 GNSS、执行器、HIL/REAL 或人工 Fly View 门。

### QA22 goal acceptance（2026-08-31 当前指针）

当前 `v0.3.0-pre.8` goal acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260831-final-qa22.json`，SHA-256
`F38EE2065DCB57A1CA2C12A99AD1F401DC4D48DA0F87848D5B6BBDD2913B7ACF`；脚本退出码 `0`，latest 与该文件字节一致。
核心 pytest 收集 `2012` 项，QGC 自定义回归 `64/64`；software=`pass_with_environment_gap`、
hardware=`offline_pending`、dirty=`295`、status SHA=`851a1abde3f79abd859ad032242cee0ee9bfd790663f615c3fe73b6b67d97c74`。
回放 GPS 质量要求 vAcc≤10 m，纬度/经度与 MAVLink fix/satellite 边界对齐；legacy `observed_at` 缺省、自定义
live policy 与 `position_healthy` 语义差异独立记录，不代表真实硬件通过。`depth_grid_udp` 限流窗口竞态与
Pixhawk per-source timestamp cursor 单调性均有回归覆盖。

现场链路预检、JSONL 会话验证器、一键采集器与交接复核器的最新软件门补充快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260831-field-capture-verify-v14.json`，SHA-256
`68C9F93C90D3790F3AA8D2F9F11EC8715795F6A0C6CD39A46E091207C264F7EA`；脚本退出码 `0`，核心
测试收集 `2055` 项，QGC 自定义回归 `66/66`，software=`pass_with_environment_gap`、
hardware=`offline_pending`。该补充快照不改写 `multidetect-goal-acceptance-latest.json` 的
QA22 指针，也不关闭 Jetson/相机/室外 GNSS、真实执行器、HIL/REAL 或人工 Fly View 门。现场
JSONL 验证器现另外强制每条记录带有限非负事件时间且整体单调不回退，以及 GPS 与 `RANGE_STATUS` 的同目标 ID/同记录绑定；
串目标或跨记录匿名样本会保持 fail-closed。

### pre.8 outdoor piecewise calibration refresh (2026-09-01)

室外实际距离标定新增 `piecewise_linear` 运行路径。标定器对多距离真值做重复 raw-depth 合并、顺序冲突和孤立
错配剔除、PAVA 单调化，写出 schema v2 曲线文档；MetricDepth/CLI/live audit metadata 记录曲线模式、实测
raw 范围和域外外推的不确定度提升，schema v1 affine 文档继续兼容。该 Python 增量的 goal acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-outdoor-piecewise-v33.json`（SHA-256
`013307F219F289A5D7F2A6CE306E472625E2C08F6FF9F484452C708747D59A57`），核心测试收集 `2109`、QGC 自定义
`66/66`，软件=`pass_with_environment_gap`、硬件=`offline_pending`。canonical parity 文件及同字节审计快照
`artifacts/evaluation/release-parity-pre.8-20260901-outdoor-piecewise-v32.json` 的 SHA-256 为
`4ED355018B24A093FB7276E26DFACEBCCB5C826BF2DD04A6B0491EB810A91B0F`。本机尚无室外真值/Jetson/GNSS，
所以不关闭 REAL、HIL、现场距离精度或人工 Fly View 门。

现场联调可直接使用 `scripts/capture_field_session.py`（或安装入口
`multidetect-field-capture`）从运行时 AuditLog 尾随采集：

```powershell
$env:PYTHONPATH = "src"
$stamp = Get-Date -Format yyyyMMdd-HHmmss
python scripts/capture_field_session.py `
  artifacts/evaluation/live.audit.jsonl `
  --output "artifacts/evaluation/field-session-$stamp.jsonl" `
  --report-out "artifacts/evaluation/field-session-$stamp.capture.json" `
  --link-preflight "artifacts/evaluation/field-link-watch-<link-stamp>.json" `
  --require-link-preflight `
  --require-target-state-evidence `
  --follow --idle-timeout-s 15 --stop-state TGT
```

采集器只读完整 JSONL 行并逐条 flush/fsync；按 AuditLog `session_id` 筛选时保留原始信封，结束报告
记录整份源 SHA-256、实际消费前缀的 `source_prefix_sha256/source_prefix_bytes`、复制输出的
 `captured_output_sha256/captured_output_bytes`、停止原因、过滤/损坏行、未完成尾部和同一
`field_session_acceptance` 验证结果。指定 `--link-preflight` 后还会绑定现场链路预检报告的绝对路径、SHA-256、
字节数和 `hardware_link_ready` 摘要；预检失败、文件被改写或缺少必需路径都会关闭报告的 `gate_passed`。
`--require-target-state-evidence` 会把传感器样本关联到最近的 `DET/TRK/LCK/TGT` 上下文，并要求 `TGT`
上下文内同时出现新鲜合格 GPS 与 `RANGE_STATUS`；取消事件会清除旧上下文，复核器会按 capture report
中的同一开关重算，避免使用取消前或旧目标样本落图。
采集错误或验证不通过也会关闭报告的 `gate_passed`；该流程不连接
飞控、不发送 MAVLink/控制数据，也不改变 Windows portable 构建产物。已有证据文件默认不覆盖，重复运行请换
时间戳或显式 `--overwrite`。交接后可运行 `scripts/verify_field_session_capture.py <report.json> --require-gate`
（安装入口 `multidetect-field-capture-verify`）重算输出 gate 并验证源消费前缀；源日志之后追加的数据只作为
`source_changed_after_capture` 诊断，不会混入已采集证据；若存在链路绑定，复核器同时重新哈希该预检文件。

QA15 之前用当前源码完成的真实 disposable Docker/PX4/Gazebo AI armed-heartbeat 复验为
`artifacts/evaluation/simulator-docker-ai-acceptance-20260830T153110Z-qa13.json`，SHA-256
`678C233C7164E123B0272CF76CC0831F1D601797F7E66877BC6CE463F4FE619C`；容器日志 SHA-256
`04F57BBF9F7A9FEB7EF5C6990DBAF4187D2AE8B5E3C49E922E3919B568B10EBD`。该 machine gate 的
`gate_passed`、`runtime_gate_passed`、`ai_policy_gate_passed`、`telemetry.qualified`、
`armed_observed` 均为 `true`，`ai_steps=1`，容器 stop/rm 清理成功；真实 Pixhawk/Jetson/室外
GNSS、执行器、HIL/REAL、实飞和人工 Fly View 仍为 `not_claimed`。

台式机当前最新 live AI 重跑证据为
`artifacts/evaluation/simulator-docker-ai-acceptance-20260901-sim-rerun.json`（SHA-256
`80A21B60271E9AEC9C41AB9346FB01D66C93DD499B6B6AAA45A3FB70170C95FF`）。该 run 的
`runtime_gate_passed=true`、`telemetry.qualified=true`、`armed_observed=true`、
`ai_policy_gate_passed=true`、`ai_steps=1`，并在共享 `MANUAL_CONTROL` 适配器输出后完成
disarm 与容器清理；仍不替代真实 Pixhawk/Jetson/GNSS、HIL/REAL 或人工 Fly View。

### v0.3.0-pre.8 combined SIM/QGC/Mission gate — latest external rerun（2026-09-01）

在当前 portable 构建上重新执行了完整 900 帧外部 Docker PX4/Gazebo/QGC 联合门。权威
source evidence：

```text
artifacts/evaluation/combined-sim-qgc-20260901T074256Z-48728/combined-evidence.json
bytes=772,736
sha256=4A50E88770FF1B2084A92D72E228C4C1913E49E69BAAFE46BA43B742939D774F
gate_passed=true; frames=900; sensor topics ready; scenario=river_rescue_vtol_001
MC→FW→MC; 7-item INT-only Mission upload/readback/active; container cleanup=true
```

这次 run 使用九个 `30000..60000` 动态 loopback host 端口，记录了保护 `14550`、完整释放
状态、Mission-ready 后连续 type-24 barrier、原生 QGC toolbar drawer、VideoOutput/PiP/
fixed-wing machine smoke 和 Docker stop/rm。独立 Combined SIM verifier 的 33/33 checks
通过，输出为
`artifacts/evaluation/combined-sim-qgc-20260901T074256Z-48728/combined-evidence-verification.json`
（SHA-256 `B2855FD4C7B64DA178676A00B01EFB3E05969681ED01B9DE597D130883D1FCD7`）。

对应 envelope 与 E7 聚合产物：

| 文件 | SHA-256 | 结果 |
|---|---|---|
| `artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v2.json` | `F608493C7F1385D070B8CCAE903444F4028B78FF21AB30152C9C05958BC8ABC0` | source/envelope hash-bound，gate 为真 |
| `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v3.json` | `8B78B49D7F857051FB171C5A64C2DC6D920990B926004291A7472C9745F0B1D7` | pre.8 清单，SIM/S4/parity 子门通过 |
| `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v3-report.json` | `8DECBFB949C2B250311A9489CB781EC7BBA8376FF1D1C2B137F934BA14E67F99` | overall `gate_passed=false`，现场/最终 release 仍 pending |
| `artifacts/evaluation/multidetect-goal-acceptance-20260901-combined-qgc-v3.json` | `64E92ADB8B99033520EE27703BAC442505828E1EA12B3E56D211864C66EC7882` | `2271` collected，`2268 passed / 3 skipped`，QGC custom `66/66` |

该结果是 disposable software-only SIM provenance，不声明真实 Pixhawk/Jetson/相机、室外
GNSS、执行器、HIL/REAL、人工 Fly View 或实飞；版本继续为 `v0.3.0-pre.8`。此前动态端口
run 的 source/envelope 记录保留在下方，作为历史 provenance。

### v0.3.0-pre.8 combined SIM/QGC/Mission gate — dynamic loopback rerun（2026-09-01 历史记录）

端口生命周期修复后，当前权威 combined machine gate 使用动态 loopback host 端口重跑：

```text
artifacts/evaluation/combined-sim-qgc-20260901T051239Z-76220/combined-evidence.json
bytes=772,886
sha256=4DE2F6AD236EBE65F97CD9A279A101453FA9C938D45E25B9A0FB2DF0D3B7F4F1
gate_passed=true; frames=900; sensor_helper_exit=0; scenario=river_rescue_vtol_001
MC→FW→MC; 7-item INT-only Mission upload/readback/active; container cleanup=true
```

`run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 本轮从 `30000..60000` 有界分配九个互不重复
的 host TCP/UDP 端口，分配前同时做 IPv4 loopback bind 探测，容器内 PX4 输入仍为 `18570`，
受保护地面站 UDP `14550` 不参与分配。实际端口为
`38392, 32511, 46147, 30192, 55621, 30275, 49253, 53375, 31727`；证据记录
`all_test_ports_released=true`、`protected_udp_14550_unchanged=true`。Docker inspect stop/rm
路径带 `20 s` 有界超时，避免 Docker Desktop API 卡顿造成 wrapper 无界等待。

该 run 证明 900 帧传感器 transport、Mission-ready 后连续 5 个 type-24 barrier、VTOL
MC→FW→MC、原生 QGC `VideoOutput`/PiP/fixed-wing machine smoke 和容器清理。SIM envelope 与
E7/S4 绑定如下：

| 文件 | SHA-256 | 说明 |
|---|---|---|
| `artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports.json` | `86B6A7B4E220D7E05C8C9E56A678B22B269587AB758D05DE8BBEE855644FD981` | SIM source envelope，gate 为真 |
| `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound.json` | `08C3B431C57329855051BDF3D5FF6FD8F8DBE54F8D09E474EB36563ADEF2B13E` | pre.8 E7 清单，绑定 SIM/S4 |
| `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-report.json` | `5A5D7E0435F8906E692ADB3A44D6D7A972A5CCF381AC960B0E92DB3D7B123298` | SIM/S4 子门通过；整体 `gate_passed=false` |

同一 source evidence 已由独立契约复核器重新计算：
`scripts/verify_combined_sim_evidence.py` 的 33/33 checks 全部通过，验证输出为
`artifacts/evaluation/combined-sim-qgc-20260901T051239Z-76220/combined-evidence-verification.json`
（SHA-256 `E8440B7B4DDD5981A1A8B7FD279CF210AE016E1858D008B9DE8D5DD507BFFCA2`）。
此前 v2 E7 清单/报告已用 `simulator_combined` 记录绑定 source 和 release-mode
文件，SHA-256 分别为
`C026AE1A3209BFE4798DDD600A12EBC4F1684DCE49E871DF5F28D80253D5A188` 和
`47222082B48FFB8E20AB1264A78CC4C25F6FFBBD9F0321AF17C18E420A65F0FD`；
`simulator_combined.passed=true` 仅表示 software-only SIM/S4 子门通过。

`scripts/run_goal_acceptance.ps1` 也支持通过
`-CombinedSimEvidence <combined.json>` 与
`-CombinedSimReleaseModeEvidence <sim-release.json>` 把该复核结果绑定到总验收；加上
`-RequireCombinedSimEvidence` 可把 source/envelope 缺失或 verifier 失败设为硬错误，输出
节点为 `simulator_combined_acceptance`。默认不传参数时保持旧版软件验收兼容，这个绑定不
提升真实 Pixhawk/Jetson、室外 GNSS、HIL/REAL 或人工 Fly View 状态。

这是 disposable PX4/Gazebo、localhost router、同核 integrated SIM/RTSP 和 QGC 原生 Fly View
的 software-only machine gate。`operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`、
桌面 GPU 视频像素、真实 Pixhawk/Jetson/相机/室外 GNSS、执行器、HIL/REAL、实飞和生产检测
准确率均不由该 run 宣称；最终 `v0.3.0` 仍未发布。
绑定本轮只读现场预检后的总 goal-acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-post-dynamic-ports.json`（SHA-256
`BB795D37E03D55FFEC7B4C9EA742BE1F8CC06E81EBCFF4A3475A6E3C52F7738F`），核心测试 `2216`、
QGC 自定义回归 `66/66`、脚本退出码 `0`；`hardware_status=offline_pending`，不改变现场
REAL/HIL/室外 GNSS 和人工 Fly View 的待验状态。

### v0.3.0-pre.8 combined SIM/QGC/Mission gate — previous run retained（2026-09-01）

上一轮证据保留用于 provenance（PowerShell 5.1 Docker `sh -c` 引号修复和 900 帧 Mission 余量）：

```text
artifacts/evaluation/combined-sim-qgc-20260831T234111Z-53232/combined-evidence.json
bytes=771,782
sha256=5194CC77F45572483B6A17A21FC8FC0DBE222BE62B9A8EB1CC14777C1B464F6C
gate_passed=true; frames=900; sensor_helper_exit=0; scenario=river_rescue_vtol_001
MC→FW→MC; 7-item INT-only Mission upload/readback/active; container cleanup=true
```

该 run 还通过了 QGC 原生 toolbar 指示器机器 smoke、Mission-ready 后连续 5 个 type-24
ACK、原生 QQuickItem `VideoOutput`/PiP/fixed-wing 抓帧；toolbar 证据文件
`artifacts/evaluation/combined-sim-qgc-20260831T234111Z-53232/qgc-native-toolbar-indicator-evidence.json`
的 SHA-256 为 `C25CD359EFE64E8F9718457ADEAA7EC80A950B848C441E60E27B50C905D5EE49`。
这是 disposable PX4/Gazebo、localhost router、同核 integrated SIM/RTSP 和 QGC 原生
Fly View 的 software-only machine gate。`operator_visual_acceptance=not_claimed`、
`qml_rendering_proved=false`、桌面 GPU 视频像素、真实 Pixhawk/Jetson/相机/室外 GNSS、
执行器、HIL/REAL、实飞和生产检测准确率均不由该 run 宣称；最终 `v0.3.0` 仍未发布。
随后现场证据 verifier 的摘要一致性、UTF-8 BOM 兼容和 goal-acceptance 空报告参数也已回归；
对应快照为 `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-verifier-hardening-v2.json`
（SHA-256 `A0FE03A238DF5CBC84E513D560C6FFE64CFE790679D0D85DE02C00B9AD069217`），其中
`core_tests_collected=2166`、QGC 自定义回归 `66`，hardware 仍为 `offline_pending`。
本次运行刷新后的 HIL envelope 为
`artifacts/evaluation/release-mode-hil-20260901-pre.8.json`（SHA-256
`4F905FBC6221A5A7442DAA8F9507B427F7ED3782585A62625E04AA8C030BCDDA`），当前 E7 清单已重新
绑定该源哈希。

### v0.3.0-pre.6 Windows portable 制品（2026-08-26）

| 制品 | 大小 | SHA-256 |
|---|---:|---|
| `build/windows-v0.3.0-pre.6-release/Release/MultiDetectGCS.exe` | `46,551,552` bytes | `AAFA5448E74C9D5041360F4384C98E140C5197A856751768DD88135436F4C662` |
| `build/windows-v0.3.0-pre.6-release/portable-v0.3.0-pre.6/bin/MultiDetectGCS.exe` | `46,551,552` bytes | `AAFA5448E74C9D5041360F4384C98E140C5197A856751768DD88135436F4C662` |
| `build/windows-v0.3.0-pre.6-release/portable-v0.3.0-pre.6/bin/MultiDetectReplayRtspPublisher.exe` | `79,872` bytes | `33935619A2A9385E96106CC73AD9ED3F2090FC2F49C5ED2A00EC3D5732DDF387` |

portable 目录本轮共 `1,013` 个文件、`529,264,527` bytes；其中 `bin/` 为 `233` 个文件。
Release 与 portable 主 EXE 字节完全一致。该历史 pre.6 证据只覆盖 E3 build/portable 依赖闭包和软件回归；
`operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`，HIL/REAL/室外 GNSS 与
最终 `v0.3.0` 发布门继续单独计账。

该 pre.6 历史重建的主 EXE 实际哈希为
`AAFA5448E74C9D5041360F4384C98E140C5197A856751768DD88135436F4C662`（路径不变）；旧哈希仅保留为
此前 pre.6 构建记录。重建后的 exact-client 原生 QGC machine capture 为
`build/qgc-native-style-20260826-postbuild/qgc-native-style.png`，不等同 Computer Use 人工验收。

### v0.3.0-pre.6 combined SIM/QGC machine gate（2026-08-26）

历史 pre.6 的唯一权威 combined 证据为：

```text
artifacts/evaluation/combined-sim-qgc-20260826T125250Z-62292/combined-evidence.json
bytes=309,023
sha256=7D6FCD1976242C85EF783E9D812D98475E2A723BAB396241801CACDDBCA0EDFC
gate_passed=true; frames=600; mission_items=7; scenario=river_rescue_vtol_001
MC→FW→MC→land→disarm; GPS fix type=3; satellites=10; false_geolocation=0
max_home_distance=143.728 m; max_relative_altitude=24.473 m
```

该门复用了同一个原生 QGC `FlyView`/`FlyViewVideo`/`PipState`、toolbar、map/PiP surface 与
QGC controls/palette/insets；没有第二窗口、第二视频 surface、独立 simulator UI 或私有主题。
PowerShell exact-PID/HWND fallback 的原生风格捕获为
`build/qgc-native-style-20260826-current3/qgc-native-style.png`（`476,031` bytes，SHA-256
`653997AC48944DB232AFD1A8C9F5AA66C1C62436016E863F44B6D129FB1AF3E2`）。这两项是 machine smoke
和 transport/Mission 证据，仍不等同人工 Computer Use/Fly View 视觉验收；
`operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`。

同一轮后续 current4 exact-client 捕获为
`build/qgc-native-style-20260826-current4/qgc-native-style.png`（`475,725` bytes，SHA-256
`1BC638D8AC3A3AEBD9BB19206438D621A9276C2F2B76C64679EC6779EF216634`）。它与 current3
一样只是 exact QGC client machine smoke，保留
`computer_use_tool_exposed=false`、`operator_visual_acceptance=not_claimed`、
computer_use_operator_authorized=true (operator pre-approval recorded)
`qml_rendering_proved=false`，不把机器截图冒充人工 Computer Use 验收。

## 可重复构建

当前唯一源码入口是统一仓库内的 `ground-control/MultiDetectGCS`。台式机的工具链固定为
Visual Studio 2022 Build Tools/MSVC 19.44、CMake 4.4.2、Qt 6.11.1、GStreamer 1.28.4、
Python 3.12 虚拟环境和 Ninja。用户环境变量 `QT_ROOT_DIR`、`GStreamer_ROOT_DIR`、
`GSTREAMER_1_0_ROOT_MSVC_X86_64`、`GSTREAMER_1_0_ROOT_X86_64` 与 `CPM_SOURCE_CACHE`
已写入当前 Windows 用户；新开 PowerShell 后即可直接调用工具。

统一仓库提供封装好的构建入口。它会自动加载 `VsDevCmd.bat -arch=amd64`、定位 Git for
Windows 的 `patch.exe`、把 MSVC `ml64.exe` 绑定为 ASM compiler，并通过 `call qt-cmake.bat`
保证 batch 返回码能回到 PowerShell；因此不要在未加载 VS 环境的普通 PowerShell 里直接调用 Ninja：

```powershell
cd D:\Projects\Multi-Detect
pwsh -NoProfile -File .\scripts\build_multidetect_windows.ps1 `
  -Configuration Release -Parallel 2 -BuildSelfTests -InstallPortable
```

脚本默认读取 `configs/product-version.json`，无需手写 `-Version`。如需显式重复版本用于 CI 日志，
只能传 `-Version 0.3.0-pre.8`；其他值会被拒绝。

最终版本构建还必须提供四模式 E7 发布门清单。把产品清单切到 `channel=release` 后，构建脚本
会自动要求 `-ReleaseGateManifest <path>`，重新校验四份证据的 SHA-256、运行模式、同 UI/同核心/
同任务契约/同安全策略/同 PX4 Mission path parity，以及 HIL/REAL/人工 Fly View 状态；任何缺口
都会在 CMake 配置前退出。开发期如需提前在 prerelease 上演练该门，可显式加
`-RequireReleaseGate`，但 prerelease 只允许验证 `gate_passed`，不会被当作最终 release。

默认输出：

```text
build\windows-v0.3.0-pre.8-release\Release\MultiDetectGCS.exe
build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe
```

裸 `Release\MultiDetectGCS.exe` 是链接产物，旁边没有 Qt/GStreamer/QML 运行时，不能当作
可移植交付物直接双击。实际运行请使用 `portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`，该目录
包含 `Qt6SerialPort.dll`、全部 Qt QML 插件、GStreamer 1.28.4 DLL 和 MSVC 运行库。构建脚本
默认关闭 NSIS（`QGC_BUILD_INSTALLER=OFF`），因为当前交付目标是**带完整运行时目录的 EXE**，
不是单文件程序；需要安装器时再单独启用 QGC installer 配置并归档到
`artifacts/qgc/windows/`。

协议自检目标也可以由同一个入口在 VS 开发环境中增量构建：

```powershell
pwsh -NoProfile -File .\scripts\build_multidetect_windows.ps1 `
  -Configuration Release -Parallel 2 -SkipConfigure -BuildSelfTests
```

运行自检前必须让 Qt DLL 在 `PATH` 中；上述入口会自动加载 VS/Qt/GStreamer 环境。自检二进制
位于 `build\windows-v0.3.0-pre.8-release\Release\`。`-SkipConfigure` 会核对 source、
configuration、numeric version、release label 和 manifest 绝对路径；旧 v0.2.0 cache 或手工改坏的
cache 会被拒绝，而不是静默生成错误版本。构建目录、便携目录、staging、
依赖缓存、虚拟环境和 `artifacts/` 均被 Git 忽略。

### CMake GUI 配置

需要使用 CMake GUI 查看或重新生成时，填写下面两项路径：

```text
Where is the source code:  D:/Projects/Multi-Detect/ground-control/MultiDetectGCS
Where to build binaries:  D:/Projects/Multi-Detect/build/windows-v0.3.0-pre.8-release
```

点击 **Configure** 时选择 `Ninja`，平台保持默认；随后在缓存中确认或新增：

```text
CMAKE_BUILD_TYPE=Release
CMAKE_PREFIX_PATH=$env:USERPROFILE/Qt/6.11.1/msvc2022_64
CMAKE_INSTALL_PREFIX=D:/Projects/Multi-Detect/build/windows-v0.3.0-pre.8-release/staging
PATCH_EXECUTABLE=C:/Program Files/Git/usr/bin/patch.exe
CMAKE_ASM_COMPILER=C:/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/VC/Tools/MSVC/14.44.35207/bin/Hostx64/x64/ml64.exe
MULTIDETECT_PRODUCT_VERSION_MANIFEST=D:/Projects/Multi-Detect/configs/product-version.json
QGC_APP_VERSION_OVERRIDE=0.3.0
QGC_APP_RELEASE_LABEL=0.3.0-pre.8
QGC_BUILD_INSTALLER=OFF
```

`QGC_APP_VERSION_OVERRIDE` 必须保持纯数值 `0.3.0`，不要填 `0.3.0-pre.4`；人可读 prerelease
身份由 `QGC_APP_RELEASE_LABEL` 提供。正常情况下更推荐让仓库构建脚本自动传入这三个字段，避免
GUI cache 与 manifest 漂移。

Qt、GStreamer、CPM 缓存和 MSVC 路径已经写入当前用户环境，但 CMake GUI 必须在环境变量
刷新后重新打开；最稳妥的做法是关闭旧的 CMake GUI，开一个新的 PowerShell 后再启动
`cmake-gui.exe`。GUI 的 **Generate** 只负责生成 Ninja 文件，实际编译仍建议点击后使用
仓库脚本，因为脚本会自动加载 `VsDevCmd.bat -arch=amd64`，避免普通 PowerShell 缺少
MSVC `INCLUDE/LIB` 环境导致 `<utility>` 或 assembler 检测失败。

### v0.3.0-pre.4 authoritative build 与 combined SIM evidence（2026-08-26）

pre.4 的 Release/portable 构建和 combined SIM 运行已经封存；在该历史 pre.4 段内的权威运行证据为：

```text
artifacts/evaluation/combined-sim-qgc-20260826T081624Z-50796/combined-evidence.json
gate_passed=true
frames=600
scenario=river_rescue_vtol_001
```

主制品身份：

| 制品 | 大小 | SHA-256 |
|---|---:|---|
| `Release/MultiDetectGCS.exe` | `46,551,552` bytes | `B798CEE669252B3DD6F1A8F07E3D77C737C04B4D128941331CBFAFDB17B44142` |
| `portable-v0.3.0-pre.4/bin/MultiDetectGCS.exe` | `46,551,552` bytes | `B798CEE669252B3DD6F1A8F07E3D77C737C04B4D128941331CBFAFDB17B44142` |
| `portable-v0.3.0-pre.4/bin/MultiDetectReplayRtspPublisher.exe` | `79,872` bytes | `F6F8AC6DD87DAA6BA7CF477AC62BDE5889B724885AE5C9DB285A485050FEFE37` |

combined gate 使用同一原生 QGC `FlyView/FlyViewVideo/PipView/FlyViewCustomLayer`，600 个
signed type-23 runtime status 与 600 个 native post-adoption type-24 ACK 连续通过，VTOL
Mission 7-item INT-only upload/readback/commit/start、MC→FW→MC、land/disarm 和 GPS/geofence
均有证据。Multi-Detect overlay 仍只使用 QGC 原生 `QGCLabel`、`QGCButton`、`QGCPalette`、
`ScreenTools`、`PipState` 和 `VideoOutput`；没有第二窗口、第二视频 surface 或 simulator-only
theme。源码回归为 `1513 passed in 28.21s`，exit code `0`；`compileall`、13 个 PowerShell AST
与 `git diff --check` 也通过。`operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`、
`desktop_capture_gpu_surface_proved=false`，因此这不是人工 Fly View/HIL/REAL/室外 GNSS 的替代品。

### v0.3.0-pre.3 版本链、Windows E3 与 QGC E4（2026-08-25）

本轮以 `configs/product-version.json` 的 `0.3.0-pre.3 / 0.3.0rc3 / 0.3.0` 为唯一版本链，
没有复用 pre.2 的原生哈希。构建命令为：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_multidetect_windows.ps1 `
  -SkipConfigure -Configuration Release -Parallel 8 -BuildSelfTests -InstallPortable
```

| `v0.3.0-pre.3` 字段 | 验证值 |
|---|---|
| Release EXE | `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\Release\MultiDetectGCS.exe` |
| portable EXE | `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\portable-v0.3.0-pre.3\bin\MultiDetectGCS.exe` |
| Release / portable 大小 | `46,556,672` bytes |
| Release / portable SHA-256 | `5F462F8EDAD3677AE3CE88C0EC1B49CA9ECCA6B0B25A7E504A80B0BC7BBC0FD3` |
| RTSP helper | `portable-v0.3.0-pre.3/bin/MultiDetectReplayRtspPublisher.exe`, `75,264` bytes |
| RTSP helper SHA-256 | `28CD41016BA93A406FC9B9E3B98B0809B4AB8675D9A04821E1DD64C495FC5C47` |
| C++ protocol/wire self-tests | 三个目标均退出 `0` |
| fixture | `build/qgc-replay-fixture-v0.3.0-pre.3/`，3 帧，64×64，10 FPS，raw-only |
| recording manifest file SHA-256 | `3f42011103b591bd663bdf75801d15ecb1e2eeb2b09a059352aafec2c69bf69e` |

真实 QGC E4 结果保存在 `build/e4-qgc-20260825-222239/`：`e4.result.json` 的同步 gate 为
`complete=true`，video/metadata 均为 `3`，最大 ACK→metadata 偏差 `0.0 ms`，QGC peer 为
`127.0.0.1:14581`，RTSP helper exit `0`，EOS/summary 均收到。视觉验收保存在
`build/e4-qgc-visual-20260825-224648/`；临时 QGC profile 设置 `QGCQml/MainFlyWindowIsMap=false`，
使同一个 QGC Fly View 启动即显示视频主面板和地图 PiP，实际桌面截图已检查无 UDP 错误模态框。
这里的 `operator_visual_acceptance` 仍由机器 result 保持 `not_claimed`，人工检查结果写入最终
pre.3 evidence，不把 headless 日志冒充画面验收。机器证据清单为
`build/windows-v0.3.0-pre.3-release/verification/v0.3.0-pre.3-evidence.json`，SHA-256
`865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`。

- 历史 pre.3 palette rebuild 的 native-QGC style smoke：`build/e4-qgc-visual-20260826-024914/qgc-flyview-video-stable.png`。
  它来自当时的 portable binary，确认 toolbar/status、map PiP、REPLAY/M1/DEPTH 和 Jetson waiting panel 仍复用 QGC surface；
  RTSP helper 在下一帧前断开，故未计入 E4 transport gate，`operator_visual_acceptance` 继续为 `not_claimed`。

#### 最新 E4：PX4 fixed-wing SIH + signed operator metadata

随后用同一便携版 QGC 完成了真实进程的隔离 PX4 SITL/operator HIL 验收，机器证据为：

```text
artifacts/evaluation/px4-sitl-qgc-operator-acceptance-20260825T190317Z-5732.json
SHA-256: `CE79F04FABC985D7BE38CC34EAF79EA526B0284C61466142F9DA0C316526E358`
```

该 gate 的 QGC 与 pinned PX4 SIH 均退出 `0`；QGC 观察到 `5` 个 PX4 autopilot heartbeat、`37`
个认证 metadata、`30` 个 `20 Hz` tracking 包，目标池为 `3 tracks / 2 pages`，完成
`SELECT_TRK -> challenge-bound APPROVE`。禁止 PX4 消息、file-mutating FTP、system-time、
operator tunnel 转发、参数/任务/飞控/载荷写入均为 `0`，保护 UDP `14550` 未改变，临时端口
`14669/14666/14670/14667/14668/18570` 全部释放且无新 crash dump。

隔离 HIL 的关闭仍走原生 `MainWindow.qml::finishCloseProcess()`，负责 link/video teardown；
acceptance-only Windows 进程在 `QGCApplication::shutdown()` 后使用 `std::_Exit`，避免 QML 与
global singleton 在进程退出时的销毁竞态。此前连续 3 次真实 QGC + pinned PX4 SITL 运行均为
`exit_code=0`、`all_passed=true`、端口释放且无新 crash dump；随后两次 QGC palette rebuild 也通过同一门槛，
当前 `stability_runs` 共 5 次，最新结果为 `20260825T190317Z-5732`，并写入同一清单。 QGC 冷启动窗口固定为 45 秒，避免 Qt Multimedia/GStreamer 冷启动被误报为协议失败。

端口分工是 QGC signed operator listener `14669`、driver source `14666`、QGC explicit PX4
listener `14670`、只读 router `14667`、SITL telemetry `14668`、SITL input `18570`。operator
metadata 只接受严格 MAVLink 2 signing，不存在 unsigned fallback。PX4 镜像固定为
`px4io/px4-sitl@sha256:bab4270c4849b7027df4bd760c79d743d738c81d7830dde14c4cc5714f781216`；
真实 V6X/Jetson/G20/GR01 没有被接触。

UI 仍然是原生 QGC 风格：acceptance driver 位于 `custom/src/FlyViewCustomLayer.qml`，作为
已渲染 QGC Fly View 的 layer 调生产 `MultiDetectState`，复用 `FlightDisplayViewVideo`、
`PipState`、`QGCLabel`、`QGCButton`、`QGCPalette` 和 `ScreenTools`，不创建第二个窗口、
独立 simulator view 或另一套控件风格。画面截图的 `operator_visual_acceptance` 继续为
`not_claimed`，人工截图检查和机器 barrier 分开记账。

同一 prerelease 还完成了 fixed-wing SIH Mission transaction 子门：

```text
artifacts/evaluation/px4-fixed-wing-sitl-auto-mission-acceptance-20260825T170332Z-62660.json
```

3 个 item 经过 `MISSION_COUNT` 和 `MISSION_REQUEST_INT` 回读，`items_match=true`、
`hash_match=true`，canonical upload/readback SHA-256 为
`14acad2d86fa047ae8af8388f7f792a018793fa65d30c9be62e425c33b484018`。这只闭合 pinned
fixed-wing SIH 的软件 Mission 事务；Gazebo 视觉、完整 fixed-wing/VTOL Mission、真实
Pixhawk/Jetson HIL 与 REAL flight 仍是独立门。

### Gazebo Harmonic camera/telemetry 子门（pre.3）

Windows + Docker Desktop 的可重复命令：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_gazebo_sitl_camera_acceptance.ps1 `
  -Frames 30 -TimeoutSeconds 45
```

该脚本使用 digest 固定的 `px4io/px4-sitl-gazebo`、`HEADLESS=1`、PX4
`gz_x500_mono_cam` 和 `default.sdf`，从真实 Gazebo transport 捕获 camera image、CameraInfo
与 NavSat。最近一次 12 帧结果：

```text
artifacts/evaluation/gazebo-sitl-camera-acceptance-20260825T193511Z-3908.json
SHA-256: 63EC2715ABDC54929DABE6975D1014693E0C69761394F63101B0803367E88C2D
```

这个历史结果只闭合 Gazebo sensor-source 子门，不把采集器冒充成 QGC `E4 INTEGRATED`；
`counted_as_qgc_e4_gate=false`，`operator_visual_acceptance=not_claimed`。下面的独立桥接证据
才验证持久化 Gazebo pixels 进入 production perception 和真实 QGC transport。

### Gazebo pixels → production perception → QGC 同帧 transport（pre.3）

使用 `-PersistFrames` 的 capture 结果为：

```text
artifacts/evaluation/gazebo-sitl-camera-acceptance-20260825T194926Z-22752.json
result JSON SHA-256: DB7C0705E6E281745B8A05A33507058D55A31A502D6D893B3DDD2307C8178823
captured payload SHA-256: 6D677CAD22D8949BA7A8FA0BC081B0740680D118C8BD415F5759589F07847BC5
```

`scripts/create_gazebo_raw_replay_fixture.py` 将这些真实 Gazebo RGB payload 转成
raw-only recording，并仅携带只读 NavSat；它不读取 Scenario Ground Truth，也不生成硬件输出。
recording manifest 为：

```text
build/qgc-replay-fixture-gazebo-v0.3.0-pre.3/recording/recording.manifest.json
SHA-256: 87334899FB51950CE6BF2D82108BC5B7A43267B4538D8900CB0087B8F56421A4
```

随后由真实 portable `MultiDetectGCS.exe` 完成 QGC transport barrier：

```text
build/e4-gazebo-qgc-20260826/e4.result.json
SHA-256: 520B1BD6732E8FB35B1FE876E457A3C95111AB751C338287252E7E7ED3529926
```

机器结果为 `complete=true`、`12/12` video、`12/12` metadata、最大 ACK→metadata 偏差
`0.0 ms`、RTSP helper exit `0`、EOS/summary 均收到，且 Ground Truth 与硬件输出边界保持隔离。
这闭合 Gazebo image → shared production core → QGC RTSP/signed metadata 的同帧 transport
子门；不替代人工 Fly View/operator visual acceptance，`operator_visual_acceptance` 仍为
`not_claimed`。完整 fixed-wing/VTOL SIM、真实 Pixhawk/Jetson HIL、室外 GNSS/REAL flight
仍保持 pending。

### v0.3.0-pre.2 版本链与 Windows E3（2026-08-25，历史）

`v0.3.0-pre.2` 已在全新 `build/windows-v0.3.0-pre.2-release/` 中完成独立取证，未复用
pre.1 哈希。实际构建命令为：

```powershell
pwsh -NoProfile -File .\scripts\build_multidetect_windows.ps1 `
  -Configuration Release -Parallel 2 -BuildSelfTests -InstallPortable
```

版本链与软件回归也已闭合：

- CMake cache 确认 numeric `0.3.0`、release label `0.3.0-pre.2`，manifest 为
  `D:/Projects/Multi-Detect/configs/product-version.json`；
- 根目录 Python 全量套件 `1303 collected`，退出码 `0`，本轮 wall time `24.2 s`；
- QGC custom + offline 静态回归 `61 passed in 0.18 s`；
- wheel
  `D:\Projects\Multi-Detect\build\wheel-smoke-v0.3.0-pre.2\multi_detect-0.3.0rc2-py3-none-any.whl`
  为 `662,046` bytes，SHA-256
  `DD96AEA726F4BE09557CD0F701772FCF9EAB1E4F41182DE9A0587603CF72B8E2`；隔离安装后的
  metadata、`multidetect.__version__`、产品 release label 分别为
  `0.3.0rc2`、`0.3.0rc2`、`0.3.0-pre.2`。

| `v0.3.0-pre.2` E3 字段 | 验证值 |
|---|---|
| Release/Ninja 完成计数与退出码 | 2623-step 构建图完成 Release 链接，wrapper 继续完成 install/windeployqt；最终退出码 `0` |
| `MultiDetectOperatorProtocolSelfTest` | 退出码 `0` |
| `MultiDetectDepthGridProtocolSelfTest` | Python fixture 生成 `44` 个 HMAC fragment，C++ 重组往返退出码 `0` |
| Release EXE 绝对路径 | `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.2-release\Release\MultiDetectGCS.exe` |
| portable EXE 绝对路径 | `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.2-release\portable-v0.3.0-pre.2\bin\MultiDetectGCS.exe` |
| Release / portable EXE 大小 | 两者均为 `46,525,440` bytes |
| Release / portable EXE SHA-256 | 两者均为 `D5A096F69DA89CAA58E0227B85A184C228B310BD5DCA314FDE906E7379125769` |
| PE 版本 | FileVersion / ProductVersion 均为 `0.3.0.0` |
| portable 目录 | `1012` files，`529,158,529` bytes；`bin` 为 `232` files |
| 依赖闭包检查 | `62` 个 `Qt6*.dll`、`Qt6SerialPort.dll`、`43` 个具名 GStreamer DLL、QML、`qwindows` 与 `qoffscreen` 均存在 |
| 桌面启动存活与收尾 | portable `bin` 工作目录、offscreen/software backend 下存活 `15 s`；stdout/stderr 均 `0 bytes`，随后只终止该 exact process object |
| evidence JSON 路径与 SHA-256 | `build/windows-v0.3.0-pre.2-release/verification/v0.3.0-pre.2-evidence.json`；`D9D0EE425AEE6976A077B69B3EB8AD4C1C8F34FC69E1F41286AB28C9CCF5FC86` |

这个 `E3` 只证明 pre.2 Windows 原生制品可构建、依赖可加载并能启动；它不证明同步 QGC
RTSP/Fly UI 的 `E4`、Gazebo、Pixhawk/Jetson HIL、室外 GPS 或真实飞行。

### v0.3.0-pre.1 历史版本链验证（2026-08-25）

以下结果只证明 `v0.3.0-pre.1` 当时 snapshot 的版本、运行时、协议、场景与静态 UI
契约；它们与后文的 Windows
原生制品 `E3` 分开记录：

- 根目录全量 Python 回归：`1260 tests collected / exit 0`；
- Runtime/Contracts/Scenario/ObservationReplay/RawSensor 边界与版本链专项：`61 passed`，其中
  产品版本专项为 `13 passed`；
- RuntimeStatus/operator 协议专项：`92 passed`；
- QGC custom + offline 静态回归：`61 passed`；
- `ruff check src tests scripts`、Python `compileall`、仓库内 10 个 PowerShell 脚本 AST、
  仓库级 `git diff --check` 与 Markdown 本地链接解析：通过；
- PEP 440 wheel `multi_detect-0.3.0rc1-py3-none-any.whl` 已构建，并在隔离 venv 中验证 metadata、
  `multidetect.__version__` 与 `PRODUCT_VERSION` 分别为 `0.3.0rc1`、`0.3.0rc1` 和
  `0.3.0-pre.1`。

可重复非构建命令（从仓库根目录运行）：

```powershell
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'

& $python -m pytest -q `
  tests\test_clock.py tests\test_runtime.py tests\test_runtime_backend.py `
  tests\test_replay.py tests\test_scenario_manifest.py `
  tests\test_ground_truth_separation.py tests\test_autonomy_contracts.py `
  tests\test_autonomy_documents.py tests\test_product_version.py

& $python -m pytest -q `
  tests\test_operator_status.py tests\test_operator_protocol.py `
  tests\test_operator_udp.py tests\test_operator_mavlink.py tests\test_operator_bridge.py

Push-Location .\ground-control\MultiDetectGCS
try {
  & .\.venv\Scripts\python.exe -m pytest -q `
    tools\tests\test_multidetect_custom_app.py tools\tests\test_offline_mode.py
} finally {
  Pop-Location
}
```

当前机器没有可调用的 `uv` CLI，因此没有把 `uv lock --check` 写成已执行；产品版本测试会独立
解析 `uv.lock`，并确认项目包不再保留旧 `0.1.0` 第二版本源。该限制不影响已经完成的 wheel 隔离
安装验证，但后续在带 `uv` 的 CI 中仍应补跑 lock 命令。

上述自动化部分最高是 `E2 TESTED`，不会自动证明同核任务闭环。Windows 原生构建与 portable
冒烟独立记为 `E3 BUILT`：

| `v0.3.0-pre.1` 历史 E3 字段 | 验证值 |
|---|---|
| Ninja 完成计数 / 退出码 | 2623-step 构建图完成 Release 链接，wrapper 继续完成 install/windeployqt；最终退出码 `0` |
| `MultiDetectOperatorProtocolSelfTest` | 退出码 `0` |
| `MultiDetectDepthGridProtocolSelfTest` | Python 生成 `44` 个 HMAC fragment，C++ 重组往返退出码 `0` |
| portable EXE 绝对路径 | `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.1-release\portable-v0.3.0-pre.1\bin\MultiDetectGCS.exe` |
| portable EXE 大小 | `46,525,440` bytes；portable 目录总大小 `529,158,543` bytes |
| portable EXE SHA-256 | `83BBAB5BD4D0C560E07DF1BBE79DE02F7815921DF2833524A598C74087F7349B` |
| PE 版本 | FileVersion / ProductVersion 均为 `0.3.0.0` |
| 依赖闭包检查 | `bin` 共 `232` 个文件；含 `62` 个 Qt DLL、`Qt6SerialPort.dll`、`43` 个 GStreamer DLL、QML 与 `qwindows/qoffscreen` platform plugin |
| 桌面启动存活时间 / 退出状态 | 以 portable `bin` 为工作目录、offscreen/software backend 启动，进程存活 `15 s`，stderr `0 bytes`；随后只终止本次冒烟的 exact PID |

机器可读的本机证据清单是
`build\windows-v0.3.0-pre.1-release\verification\v0.3.0-pre.1-evidence.json`，SHA-256 为
`903BE4ED7C10A76E22D54B4FD5CCF76EFEB89AFC824BC9F875F6472723087B85`。它位于被 Git 忽略的
build 目录中，可由下一次构建/验收重新生成；仓库内的发布事实以本表和根 README 为准。

Jetson/Pixhawk/相机/GPS 现场证据属于 `E5/E6`，必须独立记录；Windows portable 的 `E3` 不能
替代这些硬件证据，也不能写成最终 `v0.3.0` 验收。

### v0.2.0 台式机迁移验证记录（2026-08-24）

- Release 全量构建：`2615/2615`，链接退出码 `0`。
- 根目录 Python 回归：`1183 passed`；`compileall` 与 Ruff 检查均通过。
- Python 工具回归：`562 passed, 1 skipped`。
- `MultiDetectOperatorProtocolSelfTest`：退出码 `0`。
- `MultiDetectDepthGridProtocolSelfTest`：Python 生成的认证 datagram 与 C++ 重组往返通过，退出码 `0`。
- 便携 EXE 启动冒烟：进程存活超过 12 秒后正常关闭；未再出现 `Qt6SerialPort.dll` 缺失。
- Release EXE SHA-256：`E9877CF02228F99E944F622EF27F233979AE524C312BFC96A17AC355519D69C6`。
- 验收/启动脚本已改为统一仓库路径：QGC 源码与 `.venv` 位于 `ground-control/MultiDetectGCS`，
  构建与便携目录位于根目录 `build/windows-v0.2.0-release`，不再依赖旧笔记本同级仓库。

## 历史归档

`v0.2.0` 之前的本机开发安装包只保留为历史参考，不属于当前发布线。它们置于 `artifacts/qgc/windows/archive/`，采用不含功能后缀的归档名；不应用于新的部署或验证。

| 归档文件 | SHA-256 |
| --- | --- |
| `MultiDetectGCS-v0.1.0-archive-20260722T143430.exe` | `2D4D76865AD3F603DB9C03CC7B9D7847D9CD8109A29AAE1913D280EE72AA3846` |
| `MultiDetectGCS-v0.1.0-archive-20260722T150210.exe` | `EB400961AEB478A2B63DA68DE9CC0050DE4888C334E6A71BD3AAB541F5DFE15B` |

### VTOL SIM vehicle gate（pre.3，2026-08-26）

运行命令（从仓库根目录）：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_px4_gazebo_vtol_scenario_acceptance.ps1 -ScenarioManifest .\configs\scenarios\river_rescue_vtol_001.scenario.json -EvidencePath .\artifacts\evaluation\px4-vtol-sitl-scenario-acceptance-20260826-final.json -MavlinkPort 18573
```

结果：`artifacts/evaluation/px4-vtol-sitl-scenario-acceptance-20260826-final.json`，SHA-256 `2E82C2453F2D7B28815235E02B06C793366782112E8329C0B679DC1EF01ECF6E`；容器日志 SHA-256 `022EB07077DEB3216966C5BD5C21C4CCDFB8B8942A4EB381AC386D06C7247E88`。该 gate 使用 `standard_vtol` 与 `MAVLink UDP 18573`，检查 digest/容器隔离、PX4 heartbeat、GPS、ARM、VTOL 转换和 DISARM。它是车辆动力学 SIM 子门；QGC 画面仍必须使用已构建的 portable `MultiDetectGCS.exe` 原生 Fly View，`operator_visual_acceptance` 不由该 headless runner 自动声明。

证据清单更新后的 SHA-256 为 `865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`；最终 `v0.3.0` 仍需完整 fixed-wing/VTOL Mission、人工 Fly View、真实 HIL 与 REAL/GNSS gates。

### 续验：固定翼 QGC/PX4 与 Docker Desktop 路径（2026-08-26）

`scripts/run_px4_sitl_qgc_operator_acceptance.ps1` 已在当前台式机重新执行。证据为
`artifacts/evaluation/px4-sitl-qgc-operator-acceptance-continue-20260826T090000Z.json`，
SHA-256 `8B4271BE2FDEA1A62576EE77392B458774C65440BA9D1985F23AEFEADF18F2F7`；该 run
保持 QGC/PX4 exit `0`、heartbeat `6`、signed metadata `38`、tracking `30 @ 20 Hz`、
target pool `3/2 pages`、`APPROVE` challenge binding、全写入/载荷动作 `0`、端口清理和
`14550` 不变。由于 Windows 机器的 Docker Desktop `docker.exe` 不在 PATH，wrapper 现在
优先使用 PATH，回退到标准安装路径；这只修复工具发现，不改变容器隔离或 PX4 只读策略。

当前 pre.3 evidence manifest SHA-256 为
`865E321F0F618220181BE45E2F7C7B48E467F47BC88A781DA5BA8BAE4B8F776B`。新增的根目录全量回归为
`1330 passed / exit 0`，但这仍不改变 `operator_visual_acceptance=not_claimed` 或最终版本发布门。
## v0.3.0-pre.3 原生 UI / reconnect / type 24 重构构建交接（2026-08-26）

> 前文保留的 pre.1/pre.2 制品值是历史基线；前文 pre.3 精确大小和 SHA-256 属于本轮重构前的
> 中间快照。RTSP reconnect generation 与 type 24 post-adoption ACK 合入后必须重新生成 portable，
> 不能把旧 pre.3 数值抄成最终值。

### 构建命令

对已经配置好的 `windows-v0.3.0-pre.3-release` 树执行：

```powershell
cd D:\Projects\Multi-Detect
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_multidetect_windows.ps1 `
  -SkipConfigure -Configuration Release -Parallel 2 `
  -BuildSelfTests -InstallPortable
```

最终应检查以下四个路径：

```text
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\Release\MultiDetectGCS.exe
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\portable-v0.3.0-pre.3\bin\MultiDetectGCS.exe
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\portable-v0.3.0-pre.3\bin\MultiDetectReplayRtspPublisher.exe
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\verification\v0.3.0-pre.3-evidence.json
```

只交付 portable `bin` 中的 EXE 及其依赖闭包；裸 `Release` EXE 仍只是构建产物。最终主 EXE、RTSP
helper 的大小、SHA-256、self-test 和 portable smoke 结果都必须在本次 rebuild 后写入 evidence JSON。
在该文件生成并复核之前，本节不填写估算值。

### 原生 QGC surface

- 运行界面只使用 QGC `FlyView` / `PipView` / `FlightDisplayViewVideo`；不创建第二窗口，也不构建
  simulator-only 主题或自绘 FixedWing HUD。
- PiP 只显示 read-only annotations，所有目标交互只在 full view 打开。
- 弹窗、滑块和 tool inset 使用 `QGCPopupDialog`、`QGCSlider`、`QGCToolInsets`，其余继续使用
  `QGCButton`、`QGCLabel`、`QGCPalette` 与 `ScreenTools`。
- `REPLAY` / `SIM` 和未来 `HIL` / `REAL` 共用同一 QML/controller/算法核心。

### E4 传输与重连验收语义

同帧顺序必须是：

```text
actual appsrc push -> frame_accepted
                    -> signed RuntimeStatus type 23 send
                    -> native QGC controller state adoption
                    -> authenticated matching RuntimeStatusAck type 24
```

RTSP publisher 对每个 media/appsrc 会话使用新的 monotonic generation。QGC 原生
PiP/startVideo 在首帧前或流中断开时允许重新连接；旧 media `unprepared` 只能清理自己的 identity，
不能清掉新 appsrc。重试跨 generation 时，同一 wire frame 仍只在成功 push 后记一次
`frame_accepted`。无客户端超时、push failure、非法帧、EOS 前从未建立客户端继续 fail closed。

Type 24 只证明 QGC native controller post-adoption，不证明 QML 已渲染、不证明人眼已确认，也不证明
runtime policy 已接受。最终机器记录必须继续为
`operator_visual_acceptance=not_claimed`；人工 Fly View 画面点验另行留证。

本节不声明 combined 300-frame gate 已通过。完整 REPLAY/SIM、PX4 Mission、HIL、REAL 与室外
GNSS 验收尚未全部完成，因此版本保持 `v0.3.0-pre.3`。
## v0.3.0-pre.3 历史权威 portable 与 combined gate（2026-08-26）

本节是历史 pre.3 的 authoritative release block，取代当时前面的 pre.3 中间 hash 和“最终重建待取证”状态；
当前开发线已推进到 `v0.3.0-pre.8`，当前制品见本文前面的 pre.8 release block，
但不删除 pre.1/pre.2/pre.3 历史记录。

| Portable 制品 | 大小 | SHA-256 |
| --- | ---: | --- |
| `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\portable-v0.3.0-pre.3\bin\MultiDetectGCS.exe` | `46,561,280` bytes | `4C3584F984592FDDF9114EC9C08EC3C68F86C768BFAD4EB3D0F505A263FD55FE` |
| `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.3-release\portable-v0.3.0-pre.3\bin\MultiDetectReplayRtspPublisher.exe` | `79,872` bytes | `AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F` |

权威运行目录：

```text
D:\Projects\Multi-Detect\artifacts\evaluation\combined-sim-qgc-20260826T013505Z-64000
```

`combined-evidence.json` 为 `87,623` bytes，SHA-256
`6374DB67E985BBC003682BFC9F764B416604A9C3566A4A33D0164728D44219AA`，
`gate_passed=true`。验收包括：

- 300 Gazebo frames、300 RTSP `frame_accepted`、300 contiguous signed type 23、300 contiguous
  authenticated post-adoption type 24；helper exit `0`，EOS/summary 完整。
- PX4 ARM、multicopter→fixed-wing→multicopter、DISARM；GPS qualified（fix type `3`、10 星、
  506 samples、最大跳变 `0.011 m`、无 false geolocation）。
- UDP `14550` 前后不变；全部测试端口释放；combined-owned container 停止并删除。

### Native QGC PiP machine evidence

- exact process/window：PID `40048`、HWND `0x1C534FC`；
- `WindowFromPoint(286,937)` 命中相同 HWND 且 owner PID 为 `40048`，之后才用 `SendInput` 点击；
- click UTC：`2026-08-26T01:35:40.3756314Z`；
- `QGCQml/MainFlyWindowIsMap`：`true(default) → false(explicit)`；
- QSettings before：mtime `2026-08-26T01:35:38.3155174Z`，SHA-256
  `BD2E993A77172B0668F6ADBF3DCA9F4F3C0083235F9F1B7258734F111CF9B578`；
- QSettings after：mtime `2026-08-26T01:35:40.4183819Z`，SHA-256
  `764677D852C516ACE28A070886277565D0282E8D09756B1A6A56EF96401C52B8`。

该状态变化证明 native QGC PiP swap；普通 PiP swap 不重启视频，300-frame 连续链保持完整。三张截图
来自 exact PID/HWND 的 `PrintWindow(PW_RENDERFULLCONTENT)`，但 GPU-backed surface 可能在这种抓取
方式下变黑，所以它们只属于 machine smoke：`operator_visual_acceptance=not_claimed`、
`qml_rendering_proved=false`。

该历史 pre.3 portable 当时可用于后续硬件门，但最终 `v0.3.0` 仍需真实 HIL、REAL、室外相机/GNSS、完整
PX4 Mission 与人工 Fly View 视觉验收。
## v0.3.0-pre.3 历史唯一权威 release/evidence 刷新（2026-08-26T02:08Z）

更早 `T013505Z-64000` 和 QGC `4C3584F9...` 保留为历史，但不得再用于当前 pre.3 发布核验。
唯一权威值为：

| 项 | 历史 pre.3 值 |
| --- | --- |
| Release/portable `MultiDetectGCS.exe` | `46,561,280` bytes；SHA-256 `7A4D8B90F0DF0680536B9A91BEF36177542153F97745049D83D25B13BF52554D` |
| portable RTSP helper | `79,872` bytes；SHA-256 `AB3E0043DA4A124270BEE1D5EC75A717A9845CF37CB63A8ADC8A50B4752D206F` |
| 历史 authoritative run | `D:\Projects\Multi-Detect\artifacts\evaluation\combined-sim-qgc-20260826T020829Z-57624` |
| `combined-evidence.json` | `87,530` bytes；SHA-256 `62CFCA2B42EE1A9A69676B91220476609D1505DA46DE36C187E2A85BBA14EB6F`；`gate_passed=true` |

QGC 启动前通过 `Remove-Item Env:` 真正移除了
`MULTIDETECT_DISABLE_VEHICLE_LINK`、`MULTIDETECT_HIL_UI_AUTO_EXERCISE`、
`MULTIDETECT_HIL_UI_AUTO_EXIT`，而不是写入空字符串或 `0`。运行字段为
`acceptance_only_environment_absent_before_launch=true`（`env_absent=true`）、
`hil_ui_auto_exercise_log_count=0`、`hil_ui_auto_exercise_disabled=true`。

300 帧的动态 sensor timeout 为 `180 s`。`combined-sim-qgc-20260826T020108Z-52536` 是旧固定
`100 s` timeout 暴露的历史失败（`gate_passed=false`），仅用于说明修复原因，不是成功证据。

本次 300-frame RTSP、type 23/type 24、EOS/summary 均完整，helper exit `0`。exact QGC PID/HWND
`55056 / 0x18D20DE`，普通原生 PiP swap 不重启视频。截图语义检查显示 badge
`环境 SIM · HEALTHY`，但 GPU-backed surface 的 `PrintWindow` 黑面风险仍使截图只能归类 machine
smoke；`operator_visual_acceptance=not_claimed`、`qml_rendering_proved=false`。

PX4 fixed-wing transition/return、GPS qualified、UDP `14550` unchanged、端口释放与 container 删除通过；
最终 `v0.3.0` 仍等待真实 HIL/REAL、室外硬件、完整 PX4 Mission 与人工视觉验收。

## v0.3.0-pre.4 历史交接：构建与验收记录

本节记录当时 pre.4 的版本映射；当前开发线为 `v0.3.0-pre.8`，当前制品与 combined evidence 以本文前面的 pre.8 段落为准。

当时版本映射为：

| 字段 | 值 |
| --- | --- |
| release label | `0.3.0-pre.4` |
| Python / wheel | `0.3.0rc4` |
| CMake / PE numeric | `0.3.0` |
| channel | `prerelease` |

本节保留 pre.4 源码候选阶段的历史记录；Windows build 已封存，以下是当时的实际制品路径：

```text
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.4-release\Release\MultiDetectGCS.exe
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.4-release\portable-v0.3.0-pre.4\bin\MultiDetectGCS.exe
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.4-release\portable-v0.3.0-pre.4\bin\MultiDetectReplayRtspPublisher.exe
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.4-release\verification\v0.3.0-pre.4-evidence.json
```

上述路径已由 pre.4 authoritative evidence 验证；portable closure、PE version、自检、启动冒烟、
inventory、bytes、SHA-256 和 full test counts 见本节前面的 pre.4 段。下方保留的 exact-client/VTOL
源码说明属于当时的设计交接记录，不覆盖 `combined-sim-qgc-20260826T081624Z-50796` 的真实结果。

### pre.4 原生 QGC UI hard constraint

- 产品运行面只能是上游 QGC `FlyView`/`FlyViewVideo`、`PipState`、
  `FlightDisplayViewVideo`、native toolbar/map/PiP 与 gimbal/tracking input chain。
- Multi-Detect overlay 使用 `QGCPalette`、`QGCButton`、`QGCLabel`、`QGCSlider`、
  `QGCPopupDialog`、`QGCToolInsets` 和 `ScreenTools`；PiP 只读，full view 才允许目标操作。
- 构建和审查必须拒绝 `Window`/`ApplicationWindow`、第二个视频 surface、独立 simulator、
  simulator-only QML、自绘 FixedWing HUD 或自定义主题。允许的桌面自动化只能操作同一个 exact
  QGC PID/HWND，不得产生第二套 UI。

### exact-client desktop capture 与 live-video gate

combined wrapper 的 pre.4 Stage A 从 QGC exact PID 找 owner HWND，启用 Per-Monitor V2，用
`GetClientRect`/`ClientToScreen` 得到 physical client rectangle，并验证 one-monitor bounds、外部
top-level occlusion 与 25 点 `WindowFromPoint` ownership。窗口 restore/foreground/临时 topmost 和
`DwmFlush` 后，仅以 `CopyFromScreen(SourceCopy|CaptureBlt)` 捕获 exact client rect，随后恢复 cursor、
window placement、topmost、foreground 与 DPI context。

`scripts/validate_qgc_desktop_capture.py` 在排除 QGC chrome 的 video-only ROI 上检查两帧
nonblack/nonuniform，并要求 RTSP streaming/decoding 日志与 native post-adoption type 24 sequence
至少推进 5。temporal pixel/MAD/dHash 仅作诊断，不能单独作为动态源帧证据。真实 gate 通过后也只可写：

```text
transport_bound_visible_video_surface_proved=true
exact_source_frame_identity_proved=false
qml_rendering_proved=false
operator_visual_acceptance=not_claimed
```

pre.3 的 `PrintWindow` GPU 黑面截图继续是历史 machine smoke，不得与该 exact-client desktop gate
混用，也不得补写成 operator acceptance。

### VTOL Mission combined 接口

pre.4 新增 `src/multidetect/px4_mission.py` 的 `px4-vtol-mission-int-v1` 确定性 contract/compiler/
transaction、`px4_gazebo_qgc_router.py` 的 identity-bound evaluator Mission allowlist，以及
`scripts/vtol_sitl_mission_acceptance.py` headless helper。协议固定为 canonical MAVLink 2
`MISSION_ITEM_INT`、primary Mission type、最多 64 项；Mission 必须包含 VTOL takeoff、前向转换、
route/search、loiter、回到 multicopter 和唯一 terminal VTOL land。每个 sequence 都必须由 PX4
`MISSION_REQUEST_INT` 请求，upload ACK、exact-source INT-only readback 和 canonical protocol hash
相等后才允许 commit/exact AUTO Mission start。

helper 只附着 combined wrapper 已拥有的 container/router/PX4，不创建/停止第二套 simulator，也不拥有
UI。QGC 通道继续只读；evaluator Mission type、system/component、items、commands、frames 和 coordinates
有界，`MISSION_CLEAR_ALL` 禁止。combined wrapper 已有 Mission helper、active-ready screenshot 与
evidence 编排入口；真实运行结果已由 `combined-sim-qgc-20260826T081624Z-50796` 封存。

当前 release 边界：`river_rescue_vtol_001` 的 600-frame SIM Mission machine gate 已通过；其余
SIM 场景、真实 HIL、REAL、室外相机/GNSS/飞行硬件和人工 Fly View 仍为 `PENDING`，
`operator_visual_acceptance=not_claimed`。这些门全部通过前，最终 `v0.3.0` 不发布。

## 历史 v0.3.0-pre.7 构建记录

构建命令：

```powershell
cd D:\Projects\Multi-Detect
& .\scripts\build_multidetect_windows.ps1 `
  -Configuration Release -Parallel 2 -BuildSelfTests -InstallPortable
```

已生成：

```text
build/windows-v0.3.0-pre.7-release/Release/MultiDetectGCS.exe
build/windows-v0.3.0-pre.7-release/portable-v0.3.0-pre.7/bin/MultiDetectGCS.exe
build/windows-v0.3.0-pre.7-release/portable-v0.3.0-pre.7/bin/MultiDetectReplayRtspPublisher.exe
```

`MultiDetectGCS.exe` bytes 为 `46,551,552`，Release/portable SHA-256 均为
`B24754D7751521632C3C1250D69B4476CD13B8A6DCA09248A79B26CCD93973EA`；RTSP helper bytes 为
`79,872`，SHA-256 为 `769C4B0AD3DEEAE7398927B9159296D0C2172350F218663F632D5F6689B01D9D`。

### 原生 QGC 界面约束复核

`FlyView`、`FlyViewVideo`、`PipState`、`FlightDisplayViewVideo`、native toolbar/map/PiP 仍是唯一
视觉宿主；Multi-Detect 只使用 QGC controls、palette、tool insets 和 ScreenTools。历史 pre.7 exact-client
截图保存在 `build/qgc-native-style-pre.7-20260827-034544/`，截图 SHA-256 为
`1BC638D8AC3A3AEBD9BB19206438D621A9276C2F2B76C64679EC6779EF216634`。该截图与 observation 只证明
单一 QGC 窗口的 machine smoke；当前宿主没有 Computer Use 工具，故不宣称人工视觉验收。

历史 pre.7 属于 prerelease；真实 HIL/REAL、室外硬件和人工 Fly View 验收全部通过后才能发布最终
`v0.3.0`。

操作者已明确允许 Computer Use。`scripts/capture_qgc_native_style.ps1`
的 `-ComputerUseOperatorAuthorized` 只记录这项授权；本轮线程未暴露 `@oai/sky` 运行时，
所以实际采用 exact-client fallback，保留 `computer_use_tool_exposed=false` 与
`operator_visual_acceptance=not_claimed`，不把机器截图冒充人工视觉验收。授权捕获位于
`build/qgc-native-style-cu-authorized-20260826/`，SHA-256 为
`32C6461104D01200DF89F35B45C7CE87E58814E0E3273BA2B8596AB62CDD1263`。

### v61 定位状态 exact-PID 动态机器捕获

`run_px4_sitl_qgc_operator_acceptance.ps1 -CaptureNativeVisualEvidence` 会先启动唯一原生 QGC，完成签名
选择/授权，再把 current-parity 定位 evidence 的 type 10/22 按 5 Hz 刷新 12 秒。捕获脚本通过
`-ExistingProcessId` 附着该 acceptance PID，窗口至少扩展到 `1600x900` 后抓取 exact client rect；
附着模式不关闭目标进程，QGC 在 8 秒验收延时后自行受控退出。

本机最终 portable EXE 为 46,576,640 bytes，SHA-256
`62F832239C375998E8431985DA02BB063ABEBA69BDB551E46527D2EFD97F747F`。v61 PNG 位于本地忽略目录
`artifacts/evaluation/terminal-continuation-v61/native-visual-v8/qgc-native-style.png`，SHA-256
`86D279AA8260B4AC6AA708203F5DC8499D485B30C72DA18F1EEA59BF745755D3`。它证明同一 PID/HWND 上的
动态机器可见状态，仍保留 `computer_use_tool_exposed=false`、`operator_visual_acceptance=not_claimed`。

### v64 人工视觉 review 的生成与独立复验

先打开并人工查看 v61 原始 PNG，不要使用聊天窗口里的缩放预览代替原图。复制 fail-closed 模板到
本地 evidence 目录，把每个确实看到的字段逐项改为 `true`；未看到的项目必须保留 `false` 并使用
`--decision rejected`。

```powershell
Copy-Item .\configs\qgc-visual-review-checklist.json `
  .\artifacts\evaluation\terminal-continuation-v64\qgc-visual-checklist.json

$env:PYTHONPATH = "src"
python -m multidetect.qgc_visual_acceptance create `
  --repository-root . `
  --runtime-mode hil `
  --machine-report .\artifacts\evaluation\terminal-continuation-v61\px4-sitl-qgc-geolocation-native-visual-v61-pre8-v8.json `
  --screenshot .\artifacts\evaluation\terminal-continuation-v61\native-visual-v8\qgc-native-style.png `
  --qgc-executable .\build\windows-v0.3.0-pre.8-release\Release\MultiDetectGCS.exe `
  --release-parity .\configs\release-parity.json `
  --negative-control .\artifacts\evaluation\terminal-continuation-v57\gazebo-car-near-horizon-negative-evaluation-v58-pre8-v2.json `
  --reviewer "你的姓名或审计标识" `
  --reviewed-at-utc "2026-09-05T14:30:00Z" `
  --checklist .\artifacts\evaluation\terminal-continuation-v64\qgc-visual-checklist.json `
  --decision accepted `
  --out .\artifacts\evaluation\terminal-continuation-v64\qgc-operator-visual-acceptance.json

python -m multidetect.qgc_visual_acceptance verify `
  .\artifacts\evaluation\terminal-continuation-v64\qgc-operator-visual-acceptance.json `
  --repository-root .
```

验收要求 12 项全部为真，并绑定近地平线负控的 5/5 检测、5/5 invalid range、0 假坐标。记录中的
`review_sha256` 只证明内容未被修改，不声明 reviewer 的密码学身份。当前没有 accepted record，故
`operator_visual_acceptance=not_claimed`。E7 接线已完成，但最终发布要求 replay/sim/hil/real 各自使用
独立 machine report 和 screenshot 生成一份 accepted record；同一张 v61 HIL 图片不能重复填充四种模式。

### v62 三机型 PX4/Gazebo 实际位移响应与 E7 绑定

三份 disposable Docker acceptance 均从同一 perception/AI/authority/watchdog/MAVLink 路径驱动 PX4，
并以 `GLOBAL_POSITION_INT` 报告位置计算位移：fixed-wing `33.872547 m`、multirotor `1.171039 m`、
VTOL `3.784929 m`。矩阵 artifact 的文件 SHA-256 为
`6858131CAC4FF1090FF7D7936D68D841634C6F22EA450B28D7AC9A9F014A605A`，内部稳定摘要为
`7124F746870F8CC2ADCF7A78A385EBE8190320CBAB67D972D437CAAC902C00CA`。

当前 E7 manifest/report 分别为
`artifacts/evaluation/terminal-continuation-v62/release-gate-pre.8-current-20260905T124000Z-v6-bound.json`
和对应 `-report.json`；矩阵子门 `present=true/passed=true`，总门仍为 false、32 blockers。该运行没有
读取 Gazebo truth、没有 pose injection、没有接触真实 Pixhawk，不能替代 HIL/REAL/实飞或人工验收。

### v63 三机型响应重复性与故障注入

v63 不修改 QGC 源码，因此 portable EXE 字节保持不变。桌面 Docker/PX4 对 fixed-wing、multirotor、
VTOL 各执行第二次正常响应，并为每机型执行 target-refresh dropout、heartbeat dropout、mode loss，
形成 6 positive + 9 fault。正常位移 P50/P95 分别为 `36.414368/38.702007 m`、
`2.446236/3.593912 m`、`3.951534/4.101479 m`；9 个故障均在 runtime gate 通过后由对应飞行门拒绝。

当前 campaign 文件 SHA-256 为
`10091D5B56503C9FD2FEE9840D5D4D67D6431EAE6F554216197DFDC0C207395C`，内部摘要为
`BCD78748FD9D727E0E9C91510B7F82BE6AF3BDFEEA72A0022755FC1F6643CD81`。E7 matrix/campaign 都为
`present=true/passed=true`，总体仍为 false、32 blockers。
