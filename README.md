# Multi-Detect

Multi-Detect 是一个面向多旋翼、固定翼与 VTOL 的统一机载感知、目标跟踪、测距定位、
QGroundControl 地面站交互、数字孪生和 PX4 任务编排工程。

> 当前版本为 **`v0.3.0-pre.8`**。最终 `v0.3.0` 尚未发布；真实 Jetson/相机/Pixhawk、
> 室外 GNSS 精度、HIL/REAL、三机型动态响应和人工 QGC 验收仍有门禁未关闭。任何
> software/SIM 通过记录都不能解释为实飞或生产批准。

## 从这里开始

| 需要查看的内容 | 文档 |
|---|---|
| 当前做到哪里、哪些门已通过、下一步是什么 | [当前进度](docs/current-progress.md) |
| 机器可读的当前状态 | [当前进度 JSON](docs/current-progress.json) |
| 后续每次更新必须记录什么 | [仓库更新规范](docs/development-update-policy.md) |
| 分阶段任务与验收项 | [实现路线图](docs/implementation-roadmap.md) |
| 总体模块与数据流 | [系统架构](docs/architecture.md) |
| 统一仓库目录说明 | [仓库架构](docs/repository-architecture.md) |
| REPLAY/SIM/HIL/REAL 与自动任务架构 | [自主任务架构](docs/autonomy-architecture.md) |
| 版本历史 | [CHANGELOG](CHANGELOG.md) |
| 原 README 的逐轮开发流水 | [v56 以前的历史快照](docs/history/README-development-log-through-v56.md) |

以后判断项目现状时，优先级固定为：

1. 当前源码、配置、测试与机器证据；
2. `docs/current-progress.json`；
3. `docs/current-progress.md`；
4. `CHANGELOG.md` 和专题文档；
5. 历史 README 与聊天记录。

## 当前版本

| 项目 | 值 |
|---|---|
| 产品版本 | `0.3.0-pre.8` |
| Python/wheel 版本 | `0.3.0rc8` |
| QGC/PE 数值版本 | `0.3.0` |
| 发布通道 | `prerelease` |
| 最新审计的 GitHub 基线 | `origin/main` / `b5c3f10baf4cc9febaa3a10103752f202f3b0611` |
| 当前工程迭代 | v60：定位证据进入原生 QGC 的签名 HIL 传输子门通过 |
| 最终发布状态 | 阻断；E7 `gate_passed=false`、`release_allowed=false` |
| 版本源 | [`configs/product-version.json`](configs/product-version.json) |
| 共享契约身份 | [`configs/release-parity.json`](configs/release-parity.json) |

## 已实现的主链路

```text
本地/RTSP/Replay/Gazebo 图像 + Pixhawk/Gazebo/GNSS/DEM
                         │
                         ▼
  ONNX 感知 → 多目标跟踪/ReID → TRK/LCK/TGT 与人工确认
                         │
                         ▼
  共享测距与不确定性传播 → WGS-84 目标坐标 + 1σ 圆 + 证据事件
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
  原生 MultiDetectGCS Fly View   平台无关任务计划
  视频框/状态/地图/证据卡片      → PX4 Mission 编译/反馈
             │                       │
             └──────── 安全、授权、审计门 ────────┘
```

主要能力包括：

- `REPLAY / SIM / HIL / REAL` 四种运行模式，共用 UI、核心算法、任务契约、安全策略与
  PX4 Mission 路径的哈希身份；
- RGB 本地相机、RTSP、原始回放与 Gazebo camera 输入，ONNX Runtime provider 选择、模型
  manifest、候选隔离和精度审查；
- 多目标跟踪、短时遮挡恢复、ReID、目标身份保持、TRK/LCK/TGT 和人工确认状态机；
- metric depth、相机地面射线、DEM/GNSS、时间对齐、运动补偿、残差膨胀以及区间/不确定性传播；
- GPS 合格时生成目标 WGS-84 坐标和 `1σ` 圆；GPS、测距、地形或时间不合格时 fail-closed，
  不生成假坐标；
- QGC 原生 Fly View 视频覆盖层、模式状态、地图目标、证据快照和 operator protocol；
- 平台无关任务规划、PX4 Mission 编译、Mission feedback、Mode 3 固定翼视轴控制门和动态
  flight-control attestation；
- PX4/Gazebo 三机型数字孪生、场景 manifest、真值隔离、故障注入、watchdog 和发布证据；
- G20/GR01 认证消息、只读 Pixhawk 诊断、参数备份/校验、惰性载荷 HIL 协议及完整审计；
- E7 发布门：只有版本、模型、精度、人工视觉、Mission、HIL/REAL 和硬件证据全部满足时
  才能允许最终发布。

详细成熟度和未完成项见 [当前进度](docs/current-progress.md)。

## 快速开始

### Python 环境

要求 Python 3.10 或更高版本。Windows PowerShell：

```powershell
Set-Location D:\Projects\Multi-Detect

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,vision,pixhawk,security]"

.\.venv\Scripts\multi-detect.exe --help
.\.venv\Scripts\multidetect-sim.exe --help
```

只运行开发检查：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe .\scripts\create_release_parity.py --root .
```

`create_release_parity.py` 会更新 `configs/release-parity.json`。任何被五组共享契约绑定的源码
变化都必须同时提交新的 parity，旧证据自动失效。

### v59 定位证据复核与工况矩阵

本地存在候选 ONNX、原始 Gazebo frame-set 和证据文件时，可执行：

```powershell
python .\scripts\verify_gazebo_target_geolocation_acceptance.py `
  --repository-root . `
  --evidence .\artifacts\evaluation\terminal-continuation-v57\gazebo-car-geolocation-positive-control-evaluation-v58-pre8-v4.json `
  --out .\artifacts\evaluation\terminal-continuation-v57\positive-verification-rerun.json

python .\scripts\run_gazebo_geolocation_regime_matrix.py `
  --repository-root . `
  --positive-evidence .\artifacts\evaluation\terminal-continuation-v57\gazebo-car-geolocation-positive-control-evaluation-v58-pre8-v4.json `
  --positive-evidence .\artifacts\evaluation\terminal-continuation-v59\gazebo-mid-slope-geolocation-evaluation-v59-pre8-v1.json `
  --positive-evidence .\artifacts\evaluation\terminal-continuation-v59\gazebo-high-slope-geolocation-evaluation-v59-pre8-v1.json `
  --negative-no-coordinate-evidence .\artifacts\evaluation\terminal-continuation-v57\gazebo-car-near-horizon-negative-evaluation-v58-pre8-v2.json `
  --out .\artifacts\evaluation\terminal-continuation-v59\gazebo-geolocation-regime-matrix-rerun.json
```

矩阵命令当前应退出 `0`。三个正向场景共 15 帧，平均 AGL 为约 `6.20 / 12.17 / 20.15 m`，
目标距离为 `8 / 15 / 25 m`，目标处坡度为 `0 / 4.00 / 7.97 deg`；负向近地平线场景 5/5 检测、
0/5 坐标。该结果仅关闭 software/SIM 多工况定位机器子门，不代表 QGC 人工视觉、室外 GNSS、硬件、
HIL/REAL、实飞或最终 `v0.3.0` 发布通过。

`artifacts/` 默认不进入普通 Git 提交；进度文件记录其路径、摘要、结论和声明边界。需要归档的
轻量证据必须按 [仓库更新规范](docs/development-update-policy.md) 单独评审，原始视频、模型与大型
帧集应使用外部制品存储或 Git LFS，而不是无意塞入源码历史。

### v60 原生 QGC 定位状态传输验收

已有 v59 evidence 时，可将其中最后一帧的融合测距、WGS-84 坐标和 1σ 不确定性通过现有签名
operator link 送入当前原生 EXE：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_px4_sitl_qgc_operator_acceptance.ps1 `
  -TargetGeolocationEvidencePath `
    .\artifacts\evaluation\terminal-continuation-v59\gazebo-high-slope-geolocation-evaluation-v59-pre8-v1.json
```

脚本只接受 schema v2 且 `gate_passed=true`、display-only 的定位证据，并要求 QGC 分别认证
`RANGE_STATUS` type 10 与 `TARGET_GEOLOCATION_STATUS` type 22。该机器门证明协议/原生状态采纳，
仍不替代操作者对地图点、1σ 圆和证据卡片的人工视觉签字。

### MultiDetectGCS

QGC 源码位于 `ground-control/MultiDetectGCS/`。Windows 构建和 portable 部署见
[QGC Windows 发布说明](docs/qgc-windows-release.md)。常用构建命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\build_multidetect_windows.ps1 `
  -Configuration Release -SkipConfigure -BuildSelfTests -InstallPortable -Parallel 4
```

本机当前可运行 portable 入口为：

```text
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe
```

必须从完整 `portable-v0.3.0-pre.8` 目录启动。裸 `Release\MultiDetectGCS.exe` 不是单文件发布物，
缺少同目录 Qt、QML、GStreamer、插件和翻译依赖时无法启动。`build/` 是本机构建输出，不属于
GitHub 源码仓库。

## 仓库结构

```text
Multi-Detect/
├─ src/multidetect/                 Python 生产核心、运行时、测距、定位、任务与安全
├─ ground-control/MultiDetectGCS/   定制 QGroundControl / MultiDetectGCS 源码
├─ configs/                         版本、mission、model、schema、scenario 与标定配置
├─ simulation/                      Gazebo world/model、地图、DEM 与数字孪生资产
├─ scripts/                         构建、采集、验收、证据生成和运维入口
├─ tests/                           单元、集成、协议、负向和发布门回归
├─ docs/                            当前进度、架构、现场接线、发布与专题设计
├─ examples/                        可运行示例和回放输入
├─ deploy/                          Jetson/systemd 等部署模板
├─ artifacts/                       本地评估制品；默认被 .gitignore 排除
├─ build/                           本地构建树和 portable；不提交
├─ CHANGELOG.md                     版本/迭代变更记录
└─ README.md                        稳定入口，不再堆叠逐轮流水
```

## 更新规则

从 v57 起，每个有效功能、修复或验收更新至少同时维护：

1. 源码/配置与对应测试；
2. `CHANGELOG.md` 的 `[Unreleased]`；
3. `docs/current-progress.md` 与 `docs/current-progress.json`；
4. 受影响的架构、路线图或操作文档；
5. 如果共享契约变化，重新生成 `configs/release-parity.json`；
6. 如果产生验收结论，记录命令、输入身份、SHA-256、正负门、未声明范围和复核结果。

完整规则和提交检查表见 [仓库更新规范](docs/development-update-policy.md)。聊天记录不再作为
项目状态的唯一来源。

## License

本仓库使用 MIT License，见 [LICENSE](LICENSE)。
