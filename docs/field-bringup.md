# 现场链路 Bring-up（v0.3.0-pre.8）

这份 runbook 是接通飞机 Ethernet 后的第一步。它只做网络、协议和证据检查，
不登录 Jetson、不请求 MAVLink stream、不启动 RTSP 播放，也不发送飞控控制数据。

## 预期拓扑

```text
台式机 Ethernet（辅助地址） 192.168.144.250/24
                         │
                         ├── Jetson       192.168.144.20:22
                         ├── GR01/Pixhawk 192.168.144.11:5760
                         └── 相机 RTSP    192.168.144.108:554
```

实际设备地址如果不同，必须在命令行显式覆盖；不会根据扫描结果自动改写运行配置。

## 现场报告 freshness 窗口

预检 JSON 的 hash 只能证明文件没有被替换，不能证明它仍然代表当前链路。单次报告使用
`generated_at_epoch_s`，多采样 watch 使用 `finished_at_epoch_s`；目标验收绑定默认要求
报告年龄不超过 `900` 秒。缺失、未来或过期时间戳会保持 fail-closed；freshness 通过也不
会改变 TCP、协议或 route 探测结果，端点不可达时仍是 `hardware_link_ready=false`。
发布门清单中的 `max_age_s` 与 `report_timestamp_epoch_s` 必须成对绑定；只带其中一个的
手工清单会被 evaluator 拒绝。

## v49 模式 3 Fly View 现场观察点

现场人工验收必须把 UI 文案与实际 provider 权限并排记录。目标已经 LCK、但
`live_binding_attested=false` 或 `flight_control_enabled=false` 时，预期横幅是：

```text
模式 3 目标已确认 · 飞控输出锁定
仅显示姿态建议；不会发送姿态或模式命令
```

只有 provider 自己重新收到新鲜同源 heartbeat 与 `AUTOPILOT_VERSION`、所有 profile 绑定有效且当前
输出门开启后，横幅才允许显示“模式 3 正在瞄准”。断开 heartbeat、改变稳定身份或关闭连接时，应同时
记录 provider 恢复 locked 和横幅立即退出 active。仓库中的静态测试、协议自测和 offscreen boot 已通过，
但它们不替代这项人工可见验收。

## v48 实际 provider 再认证观察点

RuntimeProfile provision 和 launcher probe 通过后，不要把静态 `profile_valid` 或
`flight_control_authorized` 当成当前链路已经可输出。实际 flight provider 是新连接，启动时预期看到：

```text
fixed_wing_aim_control_configured = true|false   # 仅表示功能是否配置
flight_control_authorized = true|false           # 静态 profile 决定
live_binding_attested = false                    # 新 provider 尚未独立证明
flight_control_enabled = false
output_locked = true
```

随后必须由该 provider 自己收到新鲜 heartbeat 与同一 source 的完整 `AUTOPILOT_VERSION`。只有 REAL
flight profile、显式 output opt-in、所有静态绑定和这次 live attestation 同时成立时，当前 enabled 才能
变为 true；HIL 始终保持 locked。未 attested 时不发送姿态/模式，也不请求测距或 RC input stream。

现场还必须实际观察一次 fail-closed 转移：停止 heartbeat 超过 freshness 窗口，或在受控台架更换稳定
身份后，确认状态立即回到 `live_binding_attested=false`、`flight_control_enabled=false`、
`output_locked=true`；恢复同一 source 只发送 heartbeat 仍不能解锁，必须重新出现同源版本帧。这个软件
状态机已有自动测试，但现场观察结果仍需写入新的 HIL/REAL 报告，不能用 v48 的 Docker/SITL 重验代替。

### 当前台式机 v33 预检快照（2026-09-04）

本机 `Ethernet` 当前为 `Up / 192.168.1.13/24`，而飞机专用网段预期为
`192.168.144.0/24`。只读接口诊断没有找到候选 aircraft 网卡或直连 route；Jetson
`192.168.144.20:22`、GR01 `192.168.144.11:5760`、相机 `192.168.144.108:554` 的 TCP
和协议探测均超时，2/2 watch 样本最终为 `hardware_link_ready=false`。现场报告是
`artifacts/evaluation/terminal-continuation-v33/field-link-preflight-live-20260904T-current.json`
（12,923 bytes，SHA-256
`2B3694E74332B96B3257F405842B3584A44AE07D5FCEA8EC5E47B07DF31E651A`），接口诊断是
`artifacts/evaluation/terminal-continuation-v33/field-link-interface-diagnostic-live-20260904T-current.json`
（4,412 bytes，SHA-256
`9800D930FD0FAF1D945420BF3849175F165DC463AEBD3308411143BDD99E9D68`）。两份文件都只记录
选择/探测事实，不改网卡/IP/路由，不登录设备、不请求 MAVLink stream、不启动 RTSP、也不发
飞控控制数据。飞机接通后必须重新生成诊断和完整 3 样本预检，再把新报告显式绑定到 E7。

离线复核一份刚生成的 watch 报告（不接触设备、不发送数据）可以使用：

```powershell
Set-Location D:\Projects\Multi-Detect
$report = "artifacts/evaluation/field-link-preflight-<stamp>.json"
& .\ground-control\MultiDetectGCS\.venv\Scripts\python.exe `
  .\scripts\verify_field_link_preflight.py $report `
  --allow-unready --max-age-s 900
```

当前台式机的 freshness 接线样本保存在
`artifacts/evaluation/terminal-continuation-v22/field-link-preflight-live-20260904c.json`，
报告 SHA-256 为
`0F211E23FD0C2671C8424C276E41993EB7DABB15059A4585A044B000D3F1D682`，复核时年龄约
20.5 秒、freshness 通过；三个设备端点仍超时，因此没有关闭现场硬件门。

最新一次接线前复核使用 v23 报告
`artifacts/evaluation/terminal-continuation-v23/field-link-preflight-live-20260904d.json`，
SHA-256 `33C5BBCC0336EC30C169F8276B15173948097EB4361F5090C2692CFA7419817A`；绑定时年龄
约 18.3 秒，freshness 通过，但 Jetson、GR01 和相机仍不可达，现场硬件门保持关闭。

随后生成的 v24 watch 报告为
`artifacts/evaluation/terminal-continuation-v24/field-link-preflight-live-20260904e.json`，
16,810 bytes，SHA-256
`2341C97D1146CC33D0734D7947A5A543D05A6D96C7E6C8B0896E1C974EA6B5F2`。离线 verifier 在
`900 s` 窗口下报告年龄约 `27.2 s`、freshness 通过；三个设备端点仍不可达，故
`hardware_link_ready=false`，不能把该报告当作室外 GNSS、Pixhawk、Jetson 或相机已就绪。

当前 v25 wrapper 复核使用新 watch 报告
`artifacts/evaluation/terminal-continuation-v25/field-link-preflight-live-20260904a.json`
（16,808 bytes，SHA-256
`53E45C0C4D7B6EB7942BB7438A90F29BC422893E3E6259F97A8A4C33EF74AB39`），在
`artifacts/evaluation/terminal-continuation-v25/e7-freshness-bound-900/` 写入 manifest/report。
绑定时 `max_age_s=900` 与 `report_timestamp_epoch_s` 成对存在，schema 校验为 `0`，报告年龄
约 `157.1 s`；设备端点和飞机网段仍不可达，故 `hardware_link_ready=false`、`gate_passed=false`。
接线后仍需现场重新生成 watch，再以同一窗口绑定，不能复用这份离线报告关闭硬件门。

随后为了更新总验收快照，重新生成 v25b watch 报告
`artifacts/evaluation/terminal-continuation-v25/field-link-preflight-live-20260904b.json`
（16,805 bytes，SHA-256
`1008D53BA3B367AC756F2120F0404BC6CD8DC50C8A8B79280E02E35053D54E6A`）。它在绑定时 freshness
约 `45.2 s`、窗口 `900 s`，但三次探测仍全部失败，`hardware_link_ready=false`；对应的
完整快照在
`artifacts/evaluation/terminal-continuation-v25/goal-acceptance/multidetect-goal-acceptance.json`，
因此现场接通后仍必须重新采集，不得把 v25b 的 freshness 记录当成设备就绪证明。

## 校准板资产（接线前可准备）

当前现场相机标定使用仓库内固定规格的 ChArUco 板：`7×5` squares、方格边长 `40 mm`、
marker 边长 `20 mm`、`DICT_5X5_100`。高分辨率打印文件为
[`charuco-7x5-40mm-20mm-4200px.png`](../configs/calibration/charuco-7x5-40mm-20mm-4200px.png)，
尺寸 `4200×3000`、文件大小 `88,844 bytes`、SHA-256
`66AAF16F10689E9EDFF68B4684128E028AD89B09AABC55AB9CDC4D02521B23AC`。当前 OpenCV
兼容探测器对原图可识别 `24` 个 ChArUco corners（最低门槛为 `12`），因此打印/显示资产
本身完整；这项自检不等同于真实相机的 20 个多姿态有效视图，也不关闭校准发布门。

实采时将该板固定在平面上，保持完整边界和哑光表面，按近/中/远距离、画面四角/中心、
左右偏航、上下俯仰和轻微滚转采集至少 `20` 个不同视图。采集入口会拒绝模糊、面积过小、
角点不足或几何重复的帧；只有 `calibrate_charuco_directory` 的所有质量门通过后，才生成
可供测距使用的 calibration JSON。

安装角 survey 必须分别测量相机相对机体的 pitch-down、yaw-right 和 roll-clockwise，并把现场工具/夹具的
保守单轴 `1σ` 写入 `boresight_sigma_deg`。运行时会把该共同数值独立施加到三轴，而不是只当作 pitch
误差；因此相机支架、机体或镜头重新安装后必须重测。接线前的软件回归只验证传播公式和越地平线拒绝，
不能替代外场控制点/RTK 真值对照，也不能用默认 `0.25°` 关闭室外定位门。

## 1. 查看网卡，不改系统

在仓库根目录执行：

```powershell
Set-Location D:\Projects\Multi-Detect

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\prepare_field_link.ps1 `
  -InterfaceAlias Ethernet -VerifyOnly
```

迁移电脑或更换网线后，如果不确定应该选哪块网卡，先运行下面的只读诊断。它会枚举物理
接口（包括 `Disconnected`）、IPv4 地址和目标网段的直连 route，并给出
`selection.status=ready|ambiguous|missing`。脚本只写诊断 JSON，不添加地址、route、DNS，
也不会自动把候选接口填入运行配置：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\inspect_field_link_interfaces.ps1 `
  -ExpectedSourceCidr 192.168.144.0/24 `
  -Output artifacts\evaluation\field-link-interface-diagnostic-$(Get-Date -Format yyyyMMdd-HHmmss).json
```

台式机迁移后的此前基线只读复核（2026-09-03 15:00 本地时间）为
[`field-link-interface-diagnostic-20260903-150022.json`](../artifacts/evaluation/field-link-interface-diagnostic-20260903-150022.json)
（4,378 bytes，SHA-256
`472E36A2D912D5B95F9B975B1F1025E35ACEF475D706C0CF71F6076850F8B628`）。报告明确记录：
`Ethernet` 为 `Up` 但只有 `192.168.1.13/24`，`Ethernet 2` 与 Wi-Fi 为
`Disconnected`，目标 `192.168.144.0/24` 没有候选接口或直连 route，
`selection.status=missing`。因此它只解释了当前预检失败的网卡层原因，不是 Jetson、Pixhawk、
相机或 GNSS 已就绪的证据；接通飞机网后应重新运行该诊断，再按选择结果显式传入
`-InterfaceAlias`。

本次继续复核（2026-09-03 20:44 本地时间）保存为
[`field-link-interface-diagnostic-20260903-desktop-pre8.json`](../artifacts/evaluation/field-link-interface-diagnostic-20260903-desktop-pre8.json)
（4,384 bytes，SHA-256
`CAB5758FA853649A74CAD3E9F7D7D3B5F718DA1F2BA8118CE58302635CFFCC6D`）。它仍记录
`Ethernet=192.168.1.13/24`、目标 `192.168.144.0/24` 无候选接口/route、
`selection.status=missing`。同一快照绑定的三采样 fail-fast 现场报告为
[`field-link-preflight-20260903-desktop-pre8-interface-bound.json`](../artifacts/evaluation/field-link-preflight-20260903-desktop-pre8-interface-bound.json)
（6,658 bytes，SHA-256
`913F6E0BB79EA0F5831B6998F8F7F9415CD380DAC6C9F00F5CA1F5A0D5A10584`）。首个样本中三台
显式设备端点均超时，route source 为 `192.168.1.13`，本地 UDP `14580/14581` 可用；
`samples_requested=3`、`samples_collected=1`、`hardware_link_ready=false`。报告离线
结构复核在 `--allow-unready` 下退出 `0`，但严格就绪门仍关闭。

接通飞机 Ethernet 后，先重复只读诊断；若结果为唯一 `ready` 接口，再用实际端点采满完整
三样本窗口（不要沿用这份 fail-fast 失败快照）：

```powershell
Set-Location D:\Projects\Multi-Detect
$diag = "artifacts/evaluation/field-link-interface-diagnostic-$(Get-Date -Format yyyyMMdd-HHmmss).json"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\inspect_field_link_interfaces.ps1 `
  -ExpectedSourceCidr 192.168.144.0/24 -Output $diag

$out = "artifacts/evaluation/field-link-preflight-$(Get-Date -Format yyyyMMdd-HHmmss).json"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_field_link_preflight.ps1 -InterfaceAlias Ethernet `
  -InterfaceDiagnostic $diag -ExpectedSourceCidr 192.168.144.0/24 `
  -JetsonHost 192.168.144.20 -Gr01Host 192.168.144.11 -CameraHost 192.168.144.108 `
  -Samples 3 -IntervalSeconds 0.5 -TimeoutSeconds 2 `
  -ProtocolChecks -RequireRoute -RequireReady -Output $out
```

如果网卡诊断仍为 `missing`，不要自动改地址或路由；由现场操作员确认网线/网口后，明确
执行 `prepare_field_link.ps1 -VerifyOnly`，需要辅助地址时再在管理员 PowerShell 中执行
文档下面的 `-Elevate` 命令。

本轮还用同一诊断快照实跑了严格 wrapper（包含协议检查、route 要求和首失败停止）：
[`field-link-preflight-20260903-desktop-pre8-wrapper-strict-r2.json`](../artifacts/evaluation/field-link-preflight-20260903-desktop-pre8-wrapper-strict-r2.json)
（7,449 bytes，SHA-256
`A3F2037643363DCCBDC5BFC83000073AB12EA382E5385F5C0CBA56A7C815EA36`）。wrapper 明确输出
`Preflight exit=1; verifier exit=1; require_ready=True`，同时保留完整 JSON 供离线复核；
这正是现场链路未就绪时的预期 fail-closed 行为。非严格诊断会保留同样的报告但返回 `0`，
适合迁移排查；严格 `-RequireReady` 返回 `1` 才能阻止后续硬件验收流程继续。

如果飞机网卡没有 `192.168.144.0/24` 地址，再由现场操作员明确执行辅助地址配置：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\prepare_field_link.ps1 `
  -InterfaceAlias Ethernet -IpAddress 192.168.144.250 -PrefixLength 24 -Elevate
```

该命令只添加 `SkipAsSource` 的辅助 IPv4 地址，不添加网关、DNS 或默认路由。撤销时：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\prepare_field_link.ps1 `
  -InterfaceAlias Ethernet -IpAddress 192.168.144.250 -PrefixLength 24 -Remove -Elevate
```

## 2. 三采样链路预检

接线后先跑严格版本，确保每个样本都能通过 TCP、SSH banner、被动 MAVLink 帧、RTSP
`OPTIONS` 和本地路由来源检查：

```powershell
$out = "artifacts/evaluation/field-link-preflight-$(Get-Date -Format yyyyMMdd-HHmmss).json"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_field_link_preflight.ps1 `
  -InterfaceAlias Ethernet `
  -ExpectedSourceCidr 192.168.144.0/24 `
  -JetsonHost 192.168.144.20 -Gr01Host 192.168.144.11 `
  -CameraHost 192.168.144.108 `
  -JetsonPort 22 -Gr01Port 5760 -RtspPort 554 `
  -MetadataPort 14580 -QgcPort 14581 `
  -Samples 3 -IntervalSeconds 0.5 -TimeoutSeconds 2 `
  -ProtocolChecks -RequireRoute -RequireReady -Output $out
```

若已经运行过上面的网卡诊断，把同一份 JSON 显式传给 wrapper，可将“网卡选择快照”与
“端口/协议预检”绑定到同一个证据文件。wrapper 会把诊断文件的 SHA-256、CIDR、候选接口
和当前 `-InterfaceAlias` 写入预检报告；离线 verifier 会重新打开原文件并逐字段比对。诊断
的 `selection.status=missing` 仍然是有效的失败诊断，不会被提升成硬件就绪：

```powershell
$diagnostic = "artifacts/evaluation/field-link-interface-diagnostic-20260903-150022.json"
$out = "artifacts/evaluation/field-link-preflight-$(Get-Date -Format yyyyMMdd-HHmmss).json"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_field_link_preflight.ps1 `
  -InterfaceAlias Ethernet `
  -InterfaceDiagnostic $diagnostic `
  -ExpectedSourceCidr 192.168.144.0/24 `
  -JetsonHost 192.168.144.20 -Gr01Host 192.168.144.11 `
  -CameraHost 192.168.144.108 `
  -Samples 3 -IntervalSeconds 0.5 -TimeoutSeconds 2 `
  -ProtocolChecks -RequireRoute -Output $out
```

`run_field_link_preflight.py` 也支持等价的 `--interface-diagnostic` 与
`--interface-alias` 参数；`verify_field_link_preflight.py --allow-unready $out` 会校验这条
hash-bound 绑定。报告仍只证明网卡/端口/协议层事实，不证明 GNSS、相机标定、执行器、HIL
或 REAL。

离线 verifier 还会对主报告使用严格 JSON 解析：重复 key、`NaN/Infinity` 和符号链接输入都会
直接判定为无效；UTF-8 BOM 仍兼容。该校验只读取文件和重算 SHA-256，不会因为验证失败而重试
网络探测或发送任何控制帧。

台式机当前绑定 smoke 证据为
[`field-link-preflight-interface-binding-smoke-20260903.json`](../artifacts/evaluation/field-link-preflight-interface-binding-smoke-20260903.json)
（4,463 bytes，SHA-256
`E96AD6241C976F85E9FCE1CFE07DCD7A6A73ADEEA0CD0B2817C09A15357278D`）。离线 verifier 退出
`0` 且 `interface_diagnostic.gate_passed=true`，但端点/路由仍不可达，
`hardware_link_ready=false`；这份证据只证明绑定链工作，不关闭现场硬件门。

严格模式只有在三个样本全部通过时返回 `0`；未就绪返回 `1`。报告随后可脱机复核：

```powershell
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
$env:PYTHONPATH = 'src'
& $python scripts\verify_field_link_preflight.py $out --allow-unready
```

`--allow-unready` 只允许验证报告结构和哈希，不把未连接的设备变成通过证据。

如果换电脑后不知道飞机实际落在哪个 IPv4 网段，可以让同一个 PowerShell 入口做一次
有界、只读的服务发现。`-DiscoverCidr` 必须显式给出范围；发现结果只写入报告，绝不会
自动改写 Jetson/GR01/相机地址，也不会把候选主机提升为 `hardware_link_ready`：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_field_link_preflight.ps1 `
  -InterfaceAlias Ethernet `
  -DiscoverCidr 192.168.1.0/24 `
  -DiscoverMaxHosts 256 `
  -DiscoverWorkers 32 `
  -DiscoverTimeoutSeconds 0.20 `
  -TimeoutSeconds 0.20 `
  -Output artifacts\evaluation\field-link-discovery-$(Get-Date -Format yyyyMMdd-HHmmss).json
```

`discovery.service_candidates` 里的地址仍需分别填回下一次显式预检的
`-JetsonHost`、`-Gr01Host` 和 `-CameraHost`；没有对应 MAVLink/RTSP 服务时不要把普通
SSH 主机当成 Jetson 或相机。当前台式机在 `192.168.1.13/24` 上运行的 wrapper 复核结果为
[`field-link-preflight-wrapper-discovery-20260902.json`](../artifacts/evaluation/field-link-preflight-wrapper-discovery-20260902.json)，
报告结构与哈希复核通过，但只发现通用 SSH 候选、没有 `5760/MAVLink` 或 `554/RTSP`，
所以 `hardware_link_ready=false` 仍然正确。

随后使用同一个 PowerShell wrapper 对整个当前 `/24` 做了有界复核，证据为
[`field-link-preflight-wrapper-discovery-lan-20260902-r1.json`](../artifacts/evaluation/field-link-preflight-wrapper-discovery-lan-20260902-r1.json)
（8,540 bytes，SHA-256 `44E30678D5CB354A54FDA8EF4FBFC11376DE289274384FC828BD7B26918EF52C`）。
扫描 `192.168.1.0/24` 的 254 台主机，命中 4 个通用 SSH banner，但没有 `5760/MAVLink`
或 `554/RTSP` 服务，所以 `hardware_link_ready=false` 仍然正确。SSH 候选不能直接当作
Jetson，必须在接通飞机网后用实际三端点重新执行 `-ProtocolChecks -RequireRoute -RequireReady`。

当前时刻又保存了一份严格单样本对照报告
[`field-link-preflight-current-20260902-continue.json`](../artifacts/evaluation/field-link-preflight-current-20260902-continue.json)
（4,397 bytes，SHA-256 `0D3F411D80F01F7D5B4D3CFA63FF1427ABBBFE9A5FC5360BB19E4BDA3B18BA36`）。
Ethernet 仍使用 `192.168.1.13/24`，三组显式飞机端点与对应 route 均不可达，只有本机
`14580/14581` UDP 端口可绑定；因此严格 wrapper 返回 `1`、`hardware_link_ready=false`。
接线后用实际三端点重跑并将这份文件作为时间基线对比，不要把它升级成 REAL 通过。

随后在本台式机上按同一参数完成了三采样 watch：
[`field-link-preflight-20260902-131515.json`](../artifacts/evaluation/field-link-preflight-20260902-131515.json)
（16,799 bytes，SHA-256
`89C5217DB61BAF0117BA9B206810AB9E1FD696B92FD5D6A595F240E8F9BB431E`）。三次
Jetson SSH、GR01 MAVLink、相机 RTSP 连接与协议检查均超时，route source 仍是本机
`192.168.1.13`（不在 `192.168.144.0/24`），只有本机 14580/14581 UDP bind 通过；因此
`hardware_link_ready=false`、严格模式返回 `1`。接通飞机 Ethernet 并配置辅助地址后，
必须对实际端点重新执行上面的 `-ProtocolChecks -RequireRoute -RequireReady`，不能把该
失败 watch 当成设备或 GNSS 证据。

在同一 `192.168.1.0/24` 范围启用 `-ProtocolChecks` 的最新只读发现报告为
[`field-link-preflight-20260902-discovery-protocol-continue.json`](../artifacts/evaluation/field-link-preflight-20260902-discovery-protocol-continue.json)
（9,402 bytes，SHA-256
`3D8CBBF0E6F2A2B40966BB31694700C394FBFF49050FA57B5DB2A792B63D95CF`）。4 个响应主机
只有 SSH banner（`192.168.1.1/.62/.63/.77`），MAVLink `5760` 和 RTSP `554` 均为 0
命中，且期望 `192.168.144.0/24` route、显式 Jetson/GR01/相机端点都未通过；因此
`protocol_checks_passed=false`、`hardware_link_ready=false`。该文件是未绑定的诊断
对照，接线后仍要使用实际端点重新运行 `-ProtocolChecks -RequireRoute -RequireReady`，
不能把普通 SSH 候选机填入设备配置。

台式机迁移后的最新协议级复核已经单独保存为
[`field-link-discovery-20260903-093500-protocol.json`](../artifacts/evaluation/field-link-discovery-20260903-093500-protocol.json)
（8,436 bytes，SHA-256
`E10B84D77128E4C0FE64FB41C9805D5757C7F5F5832086C451ABB4B653193EC4`）。该报告仍只覆盖
显式 `192.168.1.0/24` 诊断范围：扫描 `254` 个地址、发现 `4` 个响应主机、SSH 命中 `4`，
MAVLink `5760` 和 RTSP `554` 均为 `0`。SSH banner 分别为
`192.168.1.1 (OpenSSH_10.0p2 Debian)`、`192.168.1.62 (OpenSSH_10.2p1 Ubuntu)`、
`192.168.1.63 (OpenSSH_for_Windows_9.5)` 和 `192.168.1.77 (OpenSSH_9.6p1 Ubuntu)`。
这些是协议识别事实，不是 Jetson、Pixhawk 或相机身份；报告顶层
`hardware_link_ready=false`，不会被写成现场就绪。接通飞机 Ethernet 后，先确认机载网段，
再把明确的三端点填回严格的 `-ProtocolChecks -RequireRoute -RequireReady` 预检。

`run_field_link_preflight.ps1` 会把三台设备的 host 和五个端口原样转发给
`run_field_link_preflight.py`。换网段时只改这些显式参数即可；`-DiscoverCidr` 仍然只是有界
诊断，发现出的候选不会覆盖端点，也不会修改网卡、路由或运行配置。若只想验证参数转发，
可用 `-Samples 1 -IntervalSeconds 0 -TimeoutSeconds 0.2` 做短样本检查，但该结果仍不能
替代三采样协议/route 预检。现场刚接线时可额外加 `-StopOnFailure`：首个失败样本出现
后立即结束，报告仍保留 `samples_requested`、`samples_collected`、`first_failure_index`
和 `hardware_link_ready=false`，不会把不完整 watch 当成通过。
启用 `-ExpectedSourceCidr` 或 `-RequireRoute` 时，partial watch 的聚合
`route_checks_passed` 会保持 `null`，表示尚未观察完剩余样本；只有完整 watch 或单次预检
才会产生布尔路由结果。离线 verifier 在 `-AllowUnready` 下接受这种结构化失败报告，
严格 `-RequireReady` 仍然关闭设备就绪门。

本次台式机还用该 wrapper 的显式端点参数做了单样本回归，报告为
[`field-link-preflight-20260902-wrapper-explicit-after-acceptance.json`](../artifacts/evaluation/field-link-preflight-20260902-wrapper-explicit-after-acceptance.json)
（3,712 bytes，SHA-256
`6A5689AF656F6FB51DCD781A7110F296F74A4E38EA1A3492CA47FB3B251CD326`）。参数已正确落入
报告的 `target`，但当前网卡仍是 `192.168.1.13/24`，三个远端服务均超时，严格 wrapper
返回 `1`；这只是脚本参数转发证据，不改变 `hardware_link_ready=false`。

2026-09-02 17:30 又完成了一次两样本只读对照：
[`field-link-preflight-20260902-173000-manual-watchdog.json`](../artifacts/evaluation/field-link-preflight-20260902-173000-manual-watchdog.json)
（11,757 bytes，SHA-256
`5ABE546780E703740B8C1A3866F38691ADF7F8CCDF7FB60E2C8C6D7DD3A4E428`）。报告结构、路由和
watch 一致性离线 verifier 通过，但两个样本都失败；本机源地址仍为 `192.168.1.13`，不在
期望的 `192.168.144.0/24`，Jetson/GR01/RTSP 不可达，`hardware_link_ready=false`。

本次 verifier 修复后的 fail-fast 对照为
[`field-link-preflight-20260902-stop-on-failure-v3.json`](../artifacts/evaluation/field-link-preflight-20260902-stop-on-failure-v3.json)
（6,684 bytes，SHA-256
`D06796570C08FC8DB5342788D6578EEC95B1DA5FAD598FC0DA1B4A51D0AAEE5E`）。它使用相同的
三机端点和 `-ProtocolChecks`，在第一个失败样本停止：`samples_requested=3`、
`samples_collected=1`、`stop_reason=failure`、`route_checks_passed=null`；wrapper 的
结构 verifier 返回 `0`，但 `hardware_link_ready=false`，因此仍不能作为设备就绪或 GNSS
证据。接线后可直接复用这条命令并去掉 `-StopOnFailure`，采满完整窗口再执行严格门。

该报告当前已作为身份绑定写入 pre.8 E7 清单
[`release-gate-pre.8-current-20260902-field-link-watch-v3.json`](../artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-watch-v3.json)
（10,336 bytes，SHA-256
`E498B45064682280DE3F82A9BA479CD5B4BBD261BABB56D16B45CCB00638AFBA`），对应固定时间
report-only 输出为
[`release-gate-pre.8-current-20260902-field-link-watch-v3-report.json`](../artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-watch-v3-report.json)
（20,260 bytes，SHA-256
`5CF73EA62905AD41F3FABEC0C904A5C558681AB978589051C9512BAF15EE9254`）。该清单只记录
`route_checks_passed=null` 的 partial-watch 事实和报告哈希；报告未收齐样本，因此 E7 与
`hardware_link_ready` 继续保持关闭。

## 当前软件验收快照（pre.8）

现场预检之外，当前软件、SIM 和证据绑定的完整续跑保存在
[`multidetect-goal-acceptance-20260902-continue-pre8.json`](../artifacts/evaluation/multidetect-goal-acceptance-20260902-continue-pre8.json)
（39,771 bytes，SHA-256
`CEC975A754E7F4F918C50F76F92EB46D0B85D7F41645FE35BDC8F9F5E75CF7CC`）。该快照的项目测试
收集 `2418`、QGC custom `66/66`、SIM/external-AI 子门均通过，但它绑定的现场报告仍为
`hardware_link_ready=false`，所以 `hardware_status=offline_pending`。这份软件快照只用来
确认接线前的程序基线；接线后仍必须按下面步骤采集真实 `GPS_RAW_INT/GPS2_RAW`，不得用
SIM 或旧 fixture 填充室外 GNSS 证据。

watchdog v2 之后的最新软件快照为
[`multidetect-goal-acceptance-20260902-manual-watchdog-v2.json`](../artifacts/evaluation/multidetect-goal-acceptance-20260902-manual-watchdog-v2.json)
（39,946 bytes，SHA-256
`13467799B7924BED99E72DAFCEA214FBA4FFBE96C246C160B4C37772DDCE6929`）。该快照收集核心测试
`2431`、QGC custom `66/66`，并将三机型 external runtime/AI join 与独立 revalidation 重新
绑定为 `source_recomputed=true`、`gate_passed=true`；现场链路仍为 `hardware_link_ready=false`，
因此接线后必须重新采集真实 GNSS/RANGE 和目标态会话。

对应本轮源码的最新软件快照为
[`multidetect-goal-acceptance-20260902-field-link-watch-v3.json`](../artifacts/evaluation/multidetect-goal-acceptance-20260902-field-link-watch-v3.json)
（31,941 bytes，SHA-256
`528436C7B4538FE103DF6C0D91CB603CAAB9398793250003098B4E33B5265793`）。核心测试收集
`2432`、QGC custom `66/66`，software-only 子门通过；现场仍保持
`hardware_status=offline_pending`。

## 3. 通过后的只读 GNSS 观察

链路预检通过后，在单独的 MAVLink 只读端点记录原始 GPS 与融合位置。仓库内的
`capture_gnss_samples.py` 只调用 `recv_match()`，不会发控制包，并把每个
`GPS_RAW_INT`/`GPS2_RAW` 的坐标、fix、卫星数、hAcc、vAcc、速度和接收时间落盘：

```powershell
$python = '.\\ground-control\\MultiDetectGCS\\.venv\\Scripts\\python.exe'
$env:PYTHONPATH = 'src'
$gnssOut = "artifacts\\evaluation\\gnss-capture-$(Get-Date -Format yyyyMMdd-HHmmss).json"
& $python scripts\\capture_gnss_samples.py `
  --endpoint udpin:0.0.0.0:14562 `
  --duration-seconds 30 `
  --minimum-qualified-samples 10 `
  --output $gnssOut
```

采集结束后先做离线结构/计数复核；该命令不会重新打开 UDP/MAVLink 连接：

```powershell
& $python scripts\\verify_gnss_capture.py `
  $gnssOut `
  --require-gate
```

`--require-gate` 要求窗口内达到 `--minimum-qualified-samples` 且没有解码错误；不带该
参数时仍会报告质量门状态，便于保留未接通或质量不足的诊断样本。复核器会重算记录数、
合格样本数和 `decode_error_count`，篡改摘要或把非接收字段改成通过值都会失败。
采集参数可以收紧，但若把 hAcc、vAcc 或 GPS age 放宽到 `6 m`、`10 m`、`1.5 s` 之外，
报告会保留诊断记录并将 `threshold_policy_passed=false`，不会通过 canonical GNSS 质量门。

`gate_passed=true` 只表示窗口内收到了足够的合格 raw-GPS 样本；报告明确标记
`advisory_only=true`、`hardware_acceptance=not_claimed`，不能单独关闭 E7。现场目标
会话仍需使用 `scripts/capture_field_session.py` 记录 `DET → TRK → LCK → TGT`、GPS、
`RANGE_STATUS` 和取消后重捕获。只有 `GPS_RAW_INT` 的 `fix_type=3..8`、已知卫星数
`6..254`、`hAcc≤6 m`、`vAcc≤10 m` 且年龄不超过 `1.5 s` 时，目标才允许落图。

## 当前台式机基线

2026-09-02 的只读三采样报告：

```text
artifacts/evaluation/field-link-preflight-20260902-continue-v3.json
SHA-256 388FFB6AE7A0DC9C6399A8CCD24F4353B5ACCE6A9C64E175270C9A2BCE52A09E
```

该报告的 JSON 结构和哈希复核通过，但当前 Ethernet 只有 `192.168.1.13/24`，路由
来源不在 `192.168.144.0/24`，Jetson、GR01 和相机三次均超时。因此它是现场未就绪的
诊断基线，不是室外 GNSS、真实 Pixhawk、HIL 或 REAL 通过证据。接通飞机网后必须生成
新的报告，并将新的文件路径和 SHA-256 绑定到 E7 清单；旧报告不能被原地改写。

2026-09-03 00:25 的迁移后有界发现结果保存在
[`field-link-discovery-20260903-ethernet.json`](../artifacts/evaluation/field-link-discovery-20260903-ethernet.json)，
SHA-256 为
`C1D655F4A8639FF1C1469B40BC8DBC61AA439D0607733B74047074E07D3BDCE4`。扫描范围为当前
`Ethernet` 的 `192.168.1.0/24`（254 台主机，单服务超时 80 ms），发现 4 个响应主机，
但只有普通 SSH 端口命中；`5760/MAVLink` 与 `554/RTSP` 均为 0 命中。发现器是只读且
不替换显式端点，所以报告仍为 `hardware_link_ready=false`。这些 SSH 地址不得当作
Jetson；接通飞机 Ethernet 后，先确认实际网段，再对明确的 Jetson/GR01/相机地址执行
`-ProtocolChecks -RequireRoute -RequireReady` 三采样预检。

从本轮开始，`scripts/verify_field_link_preflight.py` 会同时重算报告中可选的
`discovery` 对象：CIDR 主机总数、响应主机数、每个服务的命中计数、候选地址顺序、
端口/传输类型和主机是否落在扫描范围内都必须与逐主机记录一致。发现结果仍是诊断
证据，校验器不会把它转换成 `hardware_link_ready=true`；修改计数、候选列表或加入
伪造 ready 字段的报告会在离线复核阶段失败。
本次源报告的离线复核输出为
[`field-link-discovery-20260903-ethernet-verified.json`](../artifacts/evaluation/field-link-discovery-20260903-ethernet-verified.json)，
2,378 bytes，SHA-256
`6C0AD7F2BCC97568A719DFADD8FA3BE4E0B596E8F0D0493FB5E60CEE4CC3FE73`，退出码为 `0`。

统一六层入口的台式机迁移后待办快照为
[`field-evidence-bundle-20260903-desktop-pending.json`](../artifacts/evaluation/field-evidence-bundle-20260903-desktop-pending.json)
（8,858 bytes，SHA-256
`A551FA06762C4BE8697AB358D6109F54DCBDCDAE62FC60B2943AD4468251A57B`）。它只绑定了本轮
现场链路文件；由于链路 `hardware_link_ready=false` 且其余五份真实采集文件尚未生成，
六层总门保持 `gate_passed=false`。该 report-only 快照用于迁移后的起点记录，不是 GNSS、
目标精度、HIL/REAL 或最终版本证据。

本轮再次执行了只读预检，生成
[`field-link-preflight-20260903-desktop-recheck.json`](../artifacts/evaluation/field-link-preflight-20260903-desktop-recheck.json)
（5,892 bytes，SHA-256
`3C6BF75D5D352129A77212FB284400E396EF36282317BC5C9F14F982FF96830E`）。Ethernet 仍为
`192.168.1.13/24`，目标网段 `192.168.144.0/24` 的 Jetson SSH、GR01 MAVLink 和相机
RTSP 均超时，route source 也不在期望网段，故 `hardware_link_ready=false`。对应的新
bundle 为
[`field-evidence-bundle-20260903-desktop-recheck.json`](../artifacts/evaluation/field-evidence-bundle-20260903-desktop-recheck.json)
（8,966 bytes，SHA-256
`CFC201B338A2E8A11B02CF582755E4AF1F0C062F04468B3EA070A3CD29EB88FE`）；其序列化摘要重验
`passed=true`，但六层 `gate_passed=false`，保持 fail-closed。

## PX4 固件/飞控板身份被动采集

profile 签发前先在已经会把版本帧转发给 observer 的 MAVLink endpoint 上跑：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src'
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'

& $python -m multidetect pixhawk-check `
  --endpoint 'udp:0.0.0.0:14550' `
  --samples 50 `
  --interval-seconds 0.1 `
  --expected-system-id 1 `
  --expected-autopilot px4 `
  --require-operational-state `
  --require-fresh-link `
  --require-autopilot-version
```

`AUTOPILOT_VERSION` 通常是飞控对 `MAV_CMD_REQUEST_MESSAGE` 的响应。上面的 `pixhawk-check` 不发送该
请求，也不改变参数、模式、Mission 或执行器；应由当前受控 MAVLink router/QGC 会话把已有响应复制到
observer endpoint。确认输出满足：

```text
gate_passed = true
read_only = true
messages_transmitted = 0
autopilot_version_identity.raw_device_uid_serialized = false
autopilot_version_identity.identity_hash = 64 lowercase hex
```

收不到版本帧时保持失败，不得只凭 heartbeat 手填一个 firmware hash。v46 已把该身份接入
RuntimeProfile/E7 软件契约，v47 进一步要求任何实际 HIL/REAL launcher 和 REAL output opt-in 都具备
profile/observed 两侧一致的 firmware hash；这份单独诊断输出仍只是现场前置检查，不能替代下方
provision 在自己的 probe 窗口内重新观察版本帧，也不代表真实 HIL/REAL 已通过。

## HIL/REAL Mission feedback profile 现场签发

接通 Pixhawk 后，不要手工计算 RuntimeProfile 里的 identity hash。先对本次 Mission 与实际端点运行
receive-only provision；下面以 HIL fixed-wing 为例：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src'
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
$env:MULTIDETECT_RUNTIME_PROFILE_HMAC_KEY = '<secure 32-byte-or-longer key>'

& $python -m multidetect.px4_mission_feedback_cli provision `
  --mission .\missions\fixed-wing.json `
  --endpoint udpin:0.0.0.0:14550 `
  --runtime-mode hil `
  --runtime-profile-hmac-key-env MULTIDETECT_RUNTIME_PROFILE_HMAC_KEY `
  --runtime-profile-key-id field-runtime-profile-2026-09 `
  --expected-system-id 1 `
  --expected-component-id 1 `
  --probe-duration-seconds 10 `
  --out .\profiles\hil-fixed-wing.json
```

命令会从 Mission adapter 自动判断机型，并检查新鲜 PX4 heartbeat、MAV_TYPE、system/component、
`STANDBY/ACTIVE`、实际 resolved endpoint、只读状态和零发送。它还必须在本次 probe 开始之后、完成
之前收到同一 system/component 的完整 `AUTOPILOT_VERSION`；先前缓存、错 source、缺失或未来时间都会
拒绝签发。生成的 profile 必然包含 `firmware_identity_hash`，但绝不包含原始 UID。生成文件默认不能
覆盖；更换 Pixhawk、固件、端点、airframe 或 key rotation 后需要使用新的文件名，只有确实替换同一个
批准记录时才加 `--overwrite`。

REAL profile 还需将 MAVLink signing key 的十六进制值放在独立环境变量中，并添加：

```powershell
$env:MULTIDETECT_MAVLINK_SIGNING_KEY_HEX = '<hex key>'
--runtime-signing-key-hex-env MULTIDETECT_MAVLINK_SIGNING_KEY_HEX
```

原始 HMAC/signing key 不得写进 JSON、README、命令日志或 Git。为 HIL/REAL × fixed-wing/multirotor/VTOL
分别 provision 六份 profile 后，再用同一个 profile、Mission 和 endpoint 执行 30 秒
`px4_mission_feedback_cli` v2 capture 与 `verify`。profile 签发本身不是 HIL/REAL 通过证据；只有六份
独立报告都具备新鲜 terminal Mission feedback、相同 heartbeat source、与 profile 完全一致的实时
firmware identity 和 locked output，才能提交给 E7 的 `Px4MissionFeedbackBinding`。legacy profile
虽然仍可被通用 reader 在无输出诊断路径打开，但会以
`runtime_profile_firmware_identity_missing` 明确阻断此门；v47 launcher 与 REAL output load 同样会在进入
provider 前锁死它，不能把历史兼容读取当成控制授权。v48 又要求新 provider 自己完成 fresh heartbeat +
同源 firmware live attestation；probe 的通过和旧连接 snapshot 都不能继承到输出连接。

## 下一条真实验收链

```text
field-link ready
  → 只读 GNSS observer（保存 raw GPS quality/freshness）
  → RTSP + metadata 同帧
  → DET → TRK → LCK → TGT
  → 目标真值表（RTK/测量点）
  → WGS-84 误差报告 + 独立 holdout
  → HIL / REAL / 人工 QGC Fly View
```

任何一个质量门失败都保持“不可定位”并停止向 E7 发布门传递，不使用旧坐标或估算点
补齐地图。

## 4. 六层现场证据 bundle 预检

当下面七份输入都已生成后，用统一入口做一次只读 join。它把现场链路、原始 GNSS、
`DET → TRK → LCK → TGT` 执行窗口、目标事件媒体、raw 录像/telemetry 时间轴和独立
WGS-84 精度报告绑定到同一组 SHA-256；任一文件缺失、改写、跨会话或时间轴不一致都会
保持总门关闭：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src'
$python = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
$stamp = Get-Date -Format yyyyMMdd-HHmmss
$bundleOut = "artifacts\evaluation\field-evidence-bundle-$stamp.json"
# 将下列七个变量替换为同一现场窗口的实际文件
# $fieldSessionReport = 'artifacts/evaluation/field-session-<stamp>.capture.json'
# $gnssCaptureReport = 'artifacts/evaluation/gnss-capture-<stamp>.json'
# $targetEventManifest = 'artifacts/evaluation/target-events-<stamp>/manifest.json'
# $recordingManifest = 'artifacts/evaluation/raw-recording-<stamp>/recording.manifest.json'
# $telemetrySidecar = 'artifacts/evaluation/raw-recording-<stamp>/telemetry.jsonl'
# $linkPreflightReport = 'artifacts/evaluation/field-link-preflight-<stamp>.json'
# $accuracyReport = 'artifacts/evaluation/target-accuracy-<stamp>.json'

& $python scripts\check_field_evidence_bundle.py `
  --field-session-report $fieldSessionReport `
  --gnss-capture-report $gnssCaptureReport `
  --target-event-manifest $targetEventManifest `
  --recording-manifest $recordingManifest `
  --telemetry-sidecar $telemetrySidecar `
  --link-preflight $linkPreflightReport `
  --accuracy-report $accuracyReport `
  --out $bundleOut
```

变量必须显式指向本次同一现场窗口的文件；脚本不会扫描 `artifacts/` 自动挑选旧证据，
也不会打开网络、发送 MAVLink 或产生飞控输出。默认要求 raw recording 不带 SIM
scenario provenance；若只是检查 SIM 数据轴，可加 `--allow-sim-domain`，该选项只放宽
诊断域，不改变 `release_eligible=false` 和 `hardware_acceptance=not_claimed`。

现场尚未接通时可以保留一个结构化待办报告：

```powershell
& $python scripts\check_field_evidence_bundle.py `
  --field-session-report $fieldSessionReport `
  --gnss-capture-report $gnssCaptureReport `
  --target-event-manifest $targetEventManifest `
  --recording-manifest $recordingManifest `
  --telemetry-sidecar $telemetrySidecar `
  --link-preflight $linkPreflightReport `
  --accuracy-report $accuracyReport `
  --out $bundleOut --report-only
```

`--report-only` 在门未通过时返回 `0`，但报告中的 `gate_passed=false`、逐层
`blocking_reasons` 和 `next_actions` 保持原样；严格模式在门未通过时返回 `2`。完整
bundle 也只是 hash-bound、read-only 的现场证据拼接，不会单独关闭 E7、HIL、REAL、
室外 GNSS 或最终 `v0.3.0` 发布门。
