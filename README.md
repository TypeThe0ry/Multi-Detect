# Multi-Detect

Multi-Detect 是一个面向多旋翼、固定翼与 VTOL 的统一机载感知、目标跟踪、测距定位、
QGroundControl 地面站交互、数字孪生和 PX4 任务编排工程。

> 当前版本为 **`v0.3.0-pre.8`**。最终 `v0.3.0` 尚未发布；真实 Jetson/相机/Pixhawk、
> 室外 GNSS 精度、HIL/REAL、三机型硬件响应和人工 QGC 验收仍有门禁未关闭。任何
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
| 当前工程迭代 | v142：最终 GR01 session 绑定实际执行计划 |
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

### v132–v142 认证 ACK 实链源、拓扑、新鲜度与事务化 E7 绑定

ACK bench 已绑定 route、sender、socket source、topology 和 GR01 选择/ACK；v137 将后者独立接入 E7，v138
绑定 UTC/epoch 与 900 秒有效期；v139 单命令绑定 E7，v140–v141 加入 source-bound 预演，v142 将实际 plan
写入最终 session 证据链。GR01 实链仍未验收，详见 [E7 发布门](docs/release-gate.md)。

### v119–v129 现场录像来源、转换与迁移绑定

现场 raw recording 通过 `deployment-recording-source.json` 绑定分层、人员、设备、地点、来源、UTC 时间窗和
recording hash。RTSP v3 清单另绑定真实 UTC、单调时间窗、视频 bytes/SHA、全帧 probe 和 canonical hash；
`multidetect-rtsp-to-replay` 验证原容器后自动生成不可覆盖的 camera-only raw replay。

v122–v124 负责跨机复核与一键采集；v125–v129 完成隔离部署、tar/receipt 复核、安装回执、SSH identity 与
host-key snapshot。intake/campaign/final manifest 会重开来源证明；当前仍无真实六分层录像，E7 继续阻断。
命令见 [分层回放集清单](docs/replay-set-manifest.md)。

### v109–v118 field replay campaign 与 provenance 闭环

`multidetect-field-replay-campaign` 将六分层 intake、双人标注、固定模型正式推理、独立 accuracy/threshold
复核与 fresh verification 串成可恢复状态机。四个人员角色必须互异；每层 receipt 都会重开上一层原始输入、
模型、运行参数和文件哈希，pending 或混入另一轮证据时 fail-closed。完整状态机、命令与 provenance 字段见
[当前进度](docs/current-progress.md) 和 [分层回放集清单](docs/replay-set-manifest.md)。

### v108–v118 独立人工标注闭环

`multidetect-replay-annotations` / `scripts/replay_annotation_workspace.py` 现在从 v107 intake 生成逐帧
工作区；`annotate/review` CLI 原子写入逐帧 positive/negative 与独立复核。v2 `review_content_hash`
绑定全部标注和人员/时间，复核后修改会撤销复核，直接篡改会失败；v1 仅保留历史读取兼容。

campaign 的 `annotation-ui` 仅在 fresh `annotation_required` stage 启动本机 loopback 画框工作台；随机
token/CSP、严格 JSON 与逐图 SHA 保护写入。finalize 仅在全帧完成、独立 reviewer 和内容绑定有效时原子生成
truth/review receipt/manifest，仍固定 predictions、accuracy 与 release 为 false。

v107 的六分层不可变 intake 命令、拒绝条件和 receipt 契约见
[分层回放集清单](docs/replay-set-manifest.md) 与 [当前进度](docs/current-progress.md)。

### v106 replay-set readiness 进入 E7

E7 会重开 replay-set 与 accuracy 并执行 canonical readiness；旧 SIM fixture 仍明确报告 synthetic、未复核、
缺正真值/预测及采集动作，不能靠布尔声明冒充部署数据。

### v105 当前 E7 默认证据链

`scripts/run_current_release_gate_pre8.ps1` 的固定默认输入已从过期的 SIM/PX4 摘要切换到当前可复算链：
900 帧 combined QGC/PX4 source 及其 release-mode envelope、三机型 external runtime/AI join 与
revalidation、`GLOBAL_POSITION_INT` response matrix/campaign，以及按当前 Gazebo model/root 字节重新生成的
三机型 deterministic 3-D control records。无参数运行（可只指定新输出目录与时间戳）现在可以完整生成
E7 manifest/report，不会因旧 release-parity 或 model SHA 中断。

最新 report-only 复算仍为 `gate_passed=false`、`release_allowed=false` 和 32 个阻断项；这些阻断来自
replay-set 部署域/精度评审、过期且未 ready 的真实 field link、四模式人工视觉/Mission、HIL/REAL 硬件、
引用失败证据以及最终版本通道，而不是 SIM 证据漂移。本轮未修改 C++/QML，因此 portable EXE 不重建。

### v104 E7 地址来源投影

release-gate manifest 的 `real_field_link.address_state_bound` 由 writer 从 canonical field-link verifier
结果生成。E7 evaluator 会重开父预检及其地址回执，再把实际绑定状态与 manifest 投影对账；删除或篡改
投影字段、替换嵌套地址回执都会阻断。历史 prerelease manifest 仍可读取，但最终 `0.3.0` 必须显式携带
该投影。字段为 `false` 表示网卡可能由其他受控方式配置，并不自动失败；它只负责把 provenance 状态
准确暴露给最终审核者，不能伪造为 `true`。

### v103 地址 apply 回执绑定到外场预检

field-link 预检 CLI 与 PowerShell wrapper 现在可接受 `--address-state` / `-AddressState`。它会在探测前
独立重开地址证据，要求 `mode=apply`、后置条件成功、目标接口/IP/prefix 一致，并以预检观测时间执行
最多 300 秒的新鲜度检查；随后把证据路径、字节数、SHA-256、schema 与地址身份写入预检报告。

```powershell
.\scripts\run_field_link_preflight.ps1 `
  -InterfaceAlias Ethernet `
  -ExpectedSourceCidr 192.168.144.0/24 `
  -AddressState .\artifacts\evaluation\field-link-address-apply.json `
  -AddressStateMaxAgeSeconds 300 `
  -ProtocolChecks -RequireRoute `
  -Output .\artifacts\evaluation\field-link-preflight-bound.json
```

canonical verifier 会再次重开被绑定回执，拒绝源文件漂移、binding 字段篡改、软链接、超过策略上限、
地址落在父预检目标 CIDR 之外或接口诊断 alias 不一致。地址回执只证明工作站地址配置来源，不能替代
Jetson/GR01/相机端点探测，也不会单独把 `hardware_link_ready` 提升为 true。

### v102 外场地址证据独立复验

`prepare_field_link.ps1` 的 plan、verify、what-if、apply 输出统一声明
`multidetect.field-link-address-state/v1`。仓库新增 closed JSON Schema 与无网络副作用的独立 verifier，
会重算地址/路由结构、计划是否确实需要变更、安全不变量、rollback 命令、目标接口/IP/prefix、时间新鲜度
以及 apply 后置条件。

```powershell
python .\scripts\verify_field_link_address_state.py `
  .\artifacts\evaluation\field-link-address-apply.json `
  --require-applied `
  --expected-interface-alias Ethernet `
  --expected-address 192.168.144.250 `
  --expected-prefix-length 24 `
  --max-age-s 300
```

plan/verify/what-if 可以证明文档自身有效，但只有 `mode=apply` 且持久化地址状态满足 postcondition 时才能
通过 `--require-applied`。未知字段、重复 JSON key、非有限数字、错误安全标志、错误回滚动作、地址重复、
伪造成功和过期/未来时间全部 fail-closed。

### v101 地址变更后置条件

外场地址 helper 现在只在 `New-NetIPAddress` / `Remove-NetIPAddress` 真正执行后设置 `changed=true`。
`-WhatIf` 可在非管理员会话运行，输出 `mode=what-if`、`changed=false`、
`postcondition_checked=false`，不会被误记为实际修改。

正常 apply/remove 完成后会重新读取目标地址：添加必须精确存在一条匹配地址且
`SkipAsSource=true`，移除必须不存在匹配地址。最终状态和 postcondition 先写入审计输出；条件不满足时
命令以失败结束，不能留下“命令返回即成功”的假记录。

### v100 可审计的外场网络准备

`scripts/prepare_field_link.ps1` 现在支持 `-PlanOnly` 和 write-new `-Output`。非管理员可先生成包含
拟执行动作、是否需要提权、目标地址、`SkipAsSource`、明确不改网关/DNS/静态路由及精确回滚命令的
JSON 计划；`-VerifyOnly` 也可保存同格式的只读状态。输出拒绝覆盖，实际添加或移除
`192.168.144.250/24` 仍要求显式 `-Elevate`。

外场接口盘点、地址准备、预检 wrapper/CLI、canonical verifier 与 Python 聚合实现已全部进入
`safety_policy` parity。任一现场链路权威变更都会使旧的 HIL/REAL 安全身份失效。本轮没有提升硬件、
HIL、REAL 或实飞声明。

### v99 可验证、可回滚的 index 发布

`journal-status --out` 不再在 `_write_new` 成功后直接宣告完成。它先构建稳定快照，再 write-new 写入目标，
从磁盘运行完整 `verify-index`，最后用完全相同的 `indexed_at` 再构建一次当前快照并逐字比较。这样既能发现
已写 index 自身的问题，也能发现第一次 build 与 durable write 之间出现的新 session/probe 或源文件漂移。

任一复验或重建失败时，只删除本轮确定为新建的 index 输出，不碰预存文件和 journal 证据；调用方不会收到
`gate_passed=true`。成功返回值来自落盘文件 verifier，而不是内存中的预写对象。

### v98 稳定 journal 快照与 blocked probe 封账

`blocked_probe_count` 不再是无法复验的孤立数字。每个未被成功 session 引用的
`*.online-preflight.json` 都必须通过 archived observation verifier、明确为 `gate_passed=false`，并在 index
中记录文件路径/SHA、evidence digest、观测时间、HIL/REAL 模式、机型与完整失败原因。验证 index 时会重新
打开每份失败 probe 并重算这些字段；文件被删除、替换或内容漂移后，旧 index 立即失效。未引用但实际通过
的 probe 也会被拒绝，不能伪装成 blocked 统计。

index 构建前后还会检查 capture lease 必须均为空闲，并双读 campaign status、session 文件集和 probe 文件集
的名称与内容 SHA。采集过程中、report/session/lease 尚未完成事务，或任何文件在构建时变化，均不会输出
混合时间点快照。这仍是证据一致性门，不是硬件或飞行接受证据。

### v97 恢复租约与成功 session 的严格对账

成功 `.session.json` 已 write-new 落盘、但进程在退出 capture lease 上下文前异常终止时，active lease 不会
被误当成正常释放。操作员按既有死亡 owner 门恢复后，lease 与 `recovery.json` 会进入
`recovered-leases/<lease-id>`；journal index 现在接受且只接受以下两种互斥审计来源之一：

```text
released-leases/<lease-id>/lease.json
recovered-leases/<lease-id>/{lease.json,recovery.json}
```

index 会重开归档文件，核对 lease 文件 SHA、lease digest、receipt、session 内嵌 lease；recovered 路径还会
重算 recovery digest，并核对 recovery 中的 lease ID、lease 文件 SHA、lease digest 和完整 receipt。两类
归档同时存在、均不存在、recovery 缺失/篡改，或攻击者修改字段后重新计算 digest，全部 fail-closed。
`lease-status` 也分别报告 released/recovered 归档数量。该修复只补强现场证据事务，不提升任何 HIL、REAL、
hardware、flight 或最终发布声明。

### v96 断电安全的 lease 生命周期

lease 不再先创建空的 active 目录再写文件。现在先在 `.lease-candidates/<lease-id>` 中完整写入并 fsync
`lease.json`，再将整个非空目录原子发布为 active lease；若在发布前断电，残留 candidate 可见但不阻断
下一次采集。正常释放也不再执行“先删 lease 文件、再删锁目录”，而是把整个 active 目录原子移到：

```text
released-leases/<lease-id>/lease.json
```

因此任意断电点都不会产生一个已删除身份文件却仍阻塞采集的空 active 锁。released lease 会作为成功
session 的审计源永久保留；journal index 必须重开它，核对文件 hash，并要求内容与 session 内嵌 lease
完全一致。恢复过程中若已经写完 `recovery.json`、但尚未完成目录移动，可用相同 lease ID 和相同 reason
安全续跑；不同 reason 会 fail-closed。

### v95 独占采集 lease 与 journal index

`field_covariance_session run-next` 现在会在 online probe 之前原子创建 receipt/workspace 专属 lease。同一
journal 中第二个终端无法同时抓取相同 campaign；成功、blocked probe 或正常异常退出都会核对原 lease
文档后释放。若进程非正常终止，锁不会被静默删除，先查看状态：

```powershell
python -m multidetect.field_covariance_journal lease-status `
  --receipt D:\FieldEvidence\receipts\covariance-workspace-receipt.json `
  --journal-dir D:\FieldEvidence\session-journal
```

只有同机 owner PID 被只读进程查询明确判定为死亡时，才可凭输出中的精确 `lease_id` 恢复；原
`lease.json` 和带原因的 `recovery.json` 会一起移入 `recovered-leases/<lease-id>`，不会销毁审计链：

```powershell
python -m multidetect.field_covariance_journal recover-lease `
  --receipt D:\FieldEvidence\receipts\covariance-workspace-receipt.json `
  --journal-dir D:\FieldEvidence\session-journal `
  --expected-lease-id <32位lease-id> `
  --reason "capture host restarted after confirmed process exit"
```

每个成功 session 嵌入完整 digest-bound lease，并交叉绑定 receipt、campaign 与 leaf。随时可生成或验证
连续 `1..N` 前缀 index；断号、重复、乱序、不同 receipt、report 数量不一致或 campaign 回退均拒绝：

```powershell
python -m multidetect.field_covariance_journal journal-status `
  --receipt D:\FieldEvidence\receipts\covariance-workspace-receipt.json `
  --journal-dir D:\FieldEvidence\session-journal `
  --out D:\FieldEvidence\session-journal-index.json

python -m multidetect.field_covariance_journal verify-index `
  --index D:\FieldEvidence\session-journal-index.json
```

index 与 journal 必须位于收到的 closed workspace 外；这些记录仍不提升硬件、HIL、REAL、实飞或最终
发布声明。

### v94 单叶现场采集事务

完成 v93 接收后，每次只运行一个 `run-next`。journal 必须位于收到的 workspace 外：

```powershell
python -m multidetect.field_covariance_session run-next `
  --receipt D:\FieldEvidence\receipts\covariance-workspace-receipt.json `
  --journal-dir D:\FieldEvidence\session-journal
```

命令会重验 receipt/workspace/parity，锁定唯一 `next_leaf`，运行六叶静态门和当前 Pixhawk probe，把 online
preflight 以 write-new 文件落盘并立即从磁盘独立复验，只有仍在 300 秒窗口且当前叶/身份/查询预算完全
一致时才启动一次 covariance capture。成功后重新打开不可变 report，并生成 closed-schema、digest-bound
的 `.session.json`。probe 被拒时保留诊断文件但不写成功 session record，也不启动 capture。

之后或完成更多叶后，旧 session record 仍可重验：

```powershell
python -m multidetect.field_covariance_session verify-record `
  --record D:\FieldEvidence\session-journal\<leaf>.session.json
```

该 verifier 重开 receipt、本机 workspace、campaign、归档 probe 和正式 covariance report；session record
仍只是一叶操作链记录，不能单独提升最终发布或硬件/飞行 acceptance。

### v93 第二机事务式接收与回执

第二台现场机不要直接在移动介质上采集。先把已导出的 bundle 验证并原子接收到本机目录，回执必须放在
bundle 外：

```powershell
python -m multidetect.field_covariance_workspace receive `
  --bundle E:\covariance-workspace `
  --out D:\FieldEvidence\covariance-workspace `
  --receipt D:\FieldEvidence\receipts\covariance-workspace-receipt.json
```

`receive` 先完整验证源介质，只复制 manifest 声明的 20 个文件到同盘临时目录，再验证本机副本并原子
落位。目标目录或回执已存在时拒绝覆盖；回执写失败时删除本次新副本。回执记录源/目标路径、workspace
digest、当前 parity、六叶/18 源/20 文件复验结果和下一条 argv，但所有硬件、HIL、REAL、flight claims
仍固定为 false。

源介质移除后仍可复验本机副本与回执：

```powershell
python -m multidetect.field_covariance_workspace verify-receipt `
  --receipt D:\FieldEvidence\receipts\covariance-workspace-receipt.json
```

通过只证明接收事务和静态 workspace 当前一致；随后仍须在本机运行
`preflight -> probe-next -> capture-next`。

### v92 可搬迁的六叶 covariance 工作空间

现场源准备完成后，先在原机器导出一个闭合、hash-bound 的目录：

```powershell
python -m multidetect.field_covariance_workspace export `
  --config .\configs\pixhawk-covariance-field-campaign.json `
  --out .\artifacts\handoff\covariance-workspace
```

目录固定包含 `workspace.manifest.json`、相对路径 `campaign.json`，以及 HIL/REAL × 三机型共 18 份
Mission、Mission feedback 和 signed runtime profile。导出前和原子发布前都要求六叶 source preflight
通过；manifest 绑定每份文件的路径、字节数、SHA-256、当前 `release-parity.json` 与
`px4_mission_path_hash`，额外文件、symlink、路径逃逸和内容漂移都会被拒绝。

复制整个目录到第二台使用相同源码/parity 的机器后执行：

```powershell
python -m multidetect.field_covariance_workspace verify `
  --bundle D:\FieldEvidence\covariance-workspace
```

验证返回的 `campaign_path` 才是接下来运行 `preflight -> probe-next -> capture-next` 的配置。bundle 不携带
在线 probe 或 covariance report，也不重写 COM/endpoint、飞控、固件或签名身份；第二台机器必须重新
连接并观察当前 Pixhawk，不能复用第一台机器的在线状态。

### v91 严格、过期失效的 online preflight

`probe-next` 的输出已经升级为闭合 schema-v2 observation。记录包含 UTC 观测时间与 canonical
`evidence_sha256`，并绑定当前 campaign、六叶 source preflight、Mission、Mission feedback、signed
runtime profile、online schema、runner、mode-binding authority 和五组 release parity。保存后的记录可用
独立命令重开全部来源、重算 mode binding、采样时序、identity query 预算、reasons 与 gate；默认超过
300 秒即拒绝。当前 leaf 被采集、campaign 的 `next_leaf` 前移后，旧 probe 也会按设计失效。

```powershell
python -m multidetect.field_covariance_campaign verify-probe `
  --config .\configs\pixhawk-covariance-field-campaign.json `
  --probe-report .\artifacts\evaluation\field-covariance\online-preflight.json
```

可用 `--maximum-age-seconds` 显式缩短或调整现场窗口。该文件仍固定声明
`release/hardware/HIL/REAL/flight=false`，只授权紧随其后的当前叶采集，不进入 E7。

### v90 下一叶 Pixhawk online identity probe

`probe-next` 在静态六叶 source preflight 通过后，只打开当前 `next_leaf` 的 Pixhawk transport，使用正式
covariance capture 的同一个 mode-binding authority 核对当前 resolved endpoint、HEARTBEAT 六元身份、
`AUTOPILOT_VERSION` firmware hash、transport freshness 与 operational qualification。它关闭 ranging
stream 配置；若固件身份没有被动广播，最多发送 3 次 `MAV_CMD_REQUEST_MESSAGE(512)` 查询 message 148，
并要求总发送计数与查询次数严格相等。Mission、参数、模式和执行器通道始终不启用。

```powershell
python -m multidetect.field_covariance_campaign probe-next `
  --config .\configs\pixhawk-covariance-field-campaign.json `
  --status-out .\artifacts\evaluation\field-covariance\online-preflight.json
```

正式 `capture-next` 会重新执行 online probe；失败时输出 `capture_started=false`，不启动采集子进程。
online preflight 是操作状态，不是 HIL/REAL、hardware 或 flight acceptance evidence。

### v89 六叶 covariance source preflight

现场 runner 新增独立 `preflight` 命令：它不打开 Pixhawk transport，而是一次重开六叶 executable
Mission、Mission feedback v2 和 signed runtime profile，输出逐叶 `ready` / `blocked` 清单。机型 adapter、
运行模式、configured endpoint、baud、只读/零发送、runtime profile signature/profile gate、
flight-control disabled 与 output locked 任一不一致都会阻断。`capture-next` 在构造并启动采集子进程前
强制要求六叶全部 READY；源文件在前次检查后发生变化，也会在下一次重新读取时被拒绝。

```powershell
python -m multidetect.field_covariance_campaign preflight `
  --config .\configs\pixhawk-covariance-field-campaign.json `
  --status-out .\artifacts\evaluation\field-covariance\source-preflight.json
```

这只证明静态输入可进入现场采集，不证明当前连接的 Pixhawk 身份或 HIL/REAL/硬件/实飞验收通过。

### v86 严格 covariance evidence schema

[`pixhawk-odometry-covariance-observation.schema.json`](configs/schemas/pixhawk-odometry-covariance-observation.schema.json)
把报告顶层、records、16 项 evidence diagnostics、HIL/REAL mode binding、checks、source binding 与
claims 全部固定为 Draft 2020-12 闭合对象。writer 在原子落盘前验证，verifier 在 digest 和源码重建前
再次验证；未知字段、缺字段、错误类型和非 JSON 容器都会被拒绝。motion/vertical/mission 等与本证据门
无关的 provider 内部快照不再序列化。Schema 文件 hash 进入报告，并与脚本一起进入
`px4_mission_path_hash`。当前 PX4 SITL v3 实采 223/223 条 covariance、113 条记录并独立复核通过。

### v85 当前链路与本次采集身份门禁

covariance 采集不再允许用断链后缓存、未通过 operational qualification 或采集前遗留的 firmware
身份形成通过证据。报告和 HIL/REAL `mode_binding` 都要求当前 transport/qualified link 为健康、
qualification 明确 required 且 passed、reasons 为空；`AUTOPILOT_VERSION` 的 age 必须落在本次采集
时长内。当前源码在 digest 固定的 PX4 SITL 独立进程上实采 220 条 ODOMETRY/220 条有效 covariance，
保存 111 条记录，三项新增门禁与独立 verifier 全部通过；这仍不替代真实 HIL/REAL、硬件、室外
GNSS、人工 QGC 或实飞验收。

### v84 有界 PX4 firmware 身份请求

只读 provider 可显式启用 `MAV_CMD_REQUEST_MESSAGE`，向已经通过 HEARTBEAT 绑定的 PX4 请求一次
`AUTOPILOT_VERSION` 148。每个机体身份最多尝试 3 次、间隔至少 5 秒，收到完整合法响应后立即停止；
heartbeat 身份变化会清除旧 firmware 并重新开始独立预算。诊断报告分别记录 command/message ID、
attempts、响应状态和匿名化 firmware identity hash。该请求不修改参数、模式、Mission、执行器或流速。

covariance evidence 现在要求版本请求实际发生且收到合法响应，并核对总发送数等于有界 stream 配置加
firmware 读取请求。外部 PX4/Gazebo 正控证明实际 PX4 第一次请求即响应，消除了 v83 现场采集可能因
`AUTOPILOT_VERSION` 未被周期广播而永久等待的问题。

### v83 HIL/REAL covariance 身份门

`capture_pixhawk_odometry_covariance.py` 的 HIL/REAL 模式不再只是命令行标签。它现在必须同时接收
可执行 Mission、已通过的 schema-v2 PX4 Mission feedback 和签名 runtime profile，并将当前只读
Pixhawk 会话的 endpoint/baud、HEARTBEAT 五元身份与 `AUTOPILOT_VERSION` firmware hash 逐项对齐。
任一材料缺失、模式/链路/飞控锁不一致、firmware 更换或受保护 UDP 14550 被使用，证据门都会
fail-closed。该工具只声明 covariance observation，不代替 HIL/REAL acceptance、硬件响应或实飞。

HIL 现场采集示例（REAL 时将 `hil` 改为 `real`，并使用对应 flight profile/feedback）：

```powershell
python .\scripts\capture_pixhawk_odometry_covariance.py `
  --runtime-mode hil --endpoint COM7 --baud 57600 `
  --mission .\artifacts\field\hil\executable-mission.json `
  --mission-feedback-observation .\artifacts\field\hil\px4-mission-feedback.json `
  --runtime-profile .\artifacts\field\hil\runtime-profile.json `
  --samples 200 --interval-seconds 0.1 `
  --out .\artifacts\field\hil\pixhawk-odometry-covariance.json

python .\scripts\capture_pixhawk_odometry_covariance.py `
  --verify .\artifacts\field\hil\pixhawk-odometry-covariance.json
```

### v82 PX4 estimator 姿态协方差闭环

只读 Pixhawk provider 现在请求 MAVLink `ODOMETRY`（message 331），严格绑定 HEARTBEAT 的
system/component identity、`LOCAL_NED` parent frame、允许的 `LOCAL_NED/BODY_FRD` child frame 和
250 ms 姿态时间关联。它从 21 项 upper-triangle pose covariance 提取 roll/pitch/yaw 3×3 block，验证
finite、正方差和 PSD 后输出 `1σ`；unknown、错源、错 frame、时间回退、非 PSD 或部分无效输入都
fail-closed。`BODY_FRD` twist 不再被误标为 north/east velocity。

pinned Docker/PX4/Gazebo 外部运行实采 64 条 `ODOMETRY`，64 条有效 covariance，保存 47 条变化记录；
最终原始角度方差为 `6.9689e-05 / 6.8983e-05 / 1.6177e-02 rad²`，派生 roll/pitch/heading
`1σ = 0.478° / 0.476° / 7.287°`，证明 PX4 estimator 的非零来源已走通实际 MAVLink ingress。证据
经 canonical hash、provider/script/parity source binding 和重建 verifier 复核。该结果只关闭 SIM
软件子门；HIL/REAL、真实 Pixhawk、室外 GNSS、硬件与实飞仍未声明通过。

### v81 三机型外部 covariance 证据闭环

fixed-wing、multirotor、VTOL 已分别在 pinned Docker/PX4/Gazebo 外部进程中采集 3 帧
`MDGZSIM3` 数据；三份 runtime 与 frame-set 均通过 transport、逐帧重放、hash binding 和容器清理门。
每帧同时保存原始 3×3 covariance、派生 roll/pitch/heading `1σ` 和来源。实测确认当前 Gazebo 默认
OdometryPublisher 的 9 项姿态 covariance 全为 0，因此运行时明确应用 `0.3°/0.3°/1.0°` 配置下限，
并标记 `gazebo_odometry_covariance_with_configured_floor`；这证明协议和证据链已接通，不代表 Gazebo
提供了真实 estimator uncertainty，也不提升 HIL/REAL、室外 GNSS、硬件或实飞声明。

### v80 Gazebo 姿态协方差进入共享测距

Gazebo helper 现在严格读取 `PoseWithCovariance` 的 6×6 row-major 矩阵，提取固定轴 X/Y/Z 的
3×3 姿态块；缺失、长度错误、非有限、非对称或非正半定矩阵全部 fail-closed。新的 `MDGZSIM3`
固定 276-byte header 把完整矩阵纳入整帧 SHA-256，frame manifest 与独立重放也逐项绑定它。backend
通过数值 Jacobian 将 ENU/FLU 固定轴角协方差变换为 NED/FRD roll/pitch/heading `1σ`，再应用显式
配置下限；奇异姿态发布保守的 `180°/90°/180°`，不伪造精度。producer/backend 已纳入
`core_algorithm_hash`。v81 已补齐三机型外部 v3 采集，但其软件/SIM 证据不提升现场或发布声明。

### v79 姿态不确定性来源绑定

`VehicleTelemetry` 可携带完整的 roll/pitch/heading `1σ` 与来源。Live ranging 优先使用该遥测元组，
只有四项全部缺失时才使用命令行/配置 fallback；部分字段、负值、非有限数或无来源会直接返回
`attitude_uncertainty_unavailable_or_malformed`，不会混用成假精度。VI、RGB-SLAM 和主测距都通过同一
resolver 构造姿态，`RangeSolution`/operator status 同时记录实际使用的三个 sigma 与 provenance。
v79 当时的 Gazebo wire-v2 尚未携带 `OdometryWithCovariance` 矩阵，因此该迭代明确使用配置 fallback；
这一缺口已由 v80 的 wire-v3 代码与回归关闭，并在 v81 完成三机型外部 runtime 重采。

### v78 杆臂几何不确定性端点传播

共享 multimodal ranging 不再只扰动相机射线并把杆臂固定在名义值。roll/pitch、mount 三轴和像素中心的
每个 `±1σ` 样本现在同时生成 NED 射线与 GNSS→相机杆臂端点；camera-ground slant、DEM 交点、ground
range 和 absolute bearing 共用同一几何样本。这样近距离、大杆臂或倾斜机体不会得到与零杆臂相同的
bearing sigma，也不会漏掉姿态不确定性导致的杆臂 down/水平分量变化。

### v77 严格 field-evidence bindings 合同

`field-evidence-bundle.schema.json` 不再把 `bindings` 当作任意 object。八项输入的 SHA-256/bytes、时间
策略、target-event、recording、camera-mount 和 calibration 关联都有显式类型、空值与未知字段边界；
pending 报告仍可表达空输入，而完整报告必须满足同一 strict schema。独立 verifier 继续重新打开所有输入
并比较重算结果，形成“结构验证 + 来源重算”两层防线。

### v76 目标事件标定跨证据绑定

`create_target_event_replay.py` 现在可通过 `--camera-calibration`（或 capture manifest 顶层
`camera_calibration`）读取完整严格标定，把原始字节复制为 bundle 内的 `camera-calibration.json`，并在
manifest 记录 ID、大小和 SHA-256。加载器每次重新哈希并执行完整内参语义校验；field evidence bundle
还要求该 ID/SHA-256 与物理 mount survey 引用完全一致。缺失、篡改或同 ID 不同内容都会 fail-closed。

### v75 测量设备校准证据链

`equipment-record` 自动将校准证书文件的路径/大小/SHA-256 与 issuer、有效期、设备 ID、角度/线性精度
写成严格记录。survey 会重新打开记录和证书，要求测量时间位于有效窗口、CLI 设备身份和精度与记录完全
一致；证书事后变化、过期或错设备均 fail-closed。任意 PDF 文件已不能单独冒充有效校准。

### v74 现场测量模板与预检

`camera_mount_survey.py template` 可原子生成 5–1000 行固定 v1 表头的空白工作表且默认拒绝覆盖；
`validate-raw` 在创建 survey evidence 前检查文件身份、表头、数值、范围、最小样本数和 ID 唯一性，输出
严格 schema 的只读诊断。预检通过仍保持 `hardware_acceptance=not_claimed` 与
`release_eligible=false`，不能替代第二人复核或正式 survey verifier。

### v73 原始测量 CSV 单一真源

camera-mount survey 不再由独立 `--sample` 参数计算。原始 CSV 必须使用固定 v1 表头，生成器与独立
verifier 都直接从该 hash-bound 文件解析每一行并重算统计；API 调用如同时传入 samples，必须与 CSV
逐字段完全一致，否则 `samples_match_raw_measurements=false` 并关闭门。由此避免“证据绑定文件 A、实际
计算使用参数 B”。测试 CSV 仍只验证合同，不代表真实飞机测量。

### v72 物理相机安装测量证据门

`multidetect-camera-mount-survey` 现在要求至少 5 组 pitch/yaw/roll 与 FRD forward/right/down
重复测量、测量设备校准证书、原始记录、相机 calibration 三类文件的 SHA-256 绑定、不同的 surveyor/
reviewer，以及六项物理现场声明。独立 verifier 会重新读取全部文件、重算均值/重复性/保守 `1σ`，并检查
calibration 的安装值和 uncertainty 没有低报。该报告已成为 field evidence bundle 的第七个必需层；
缺失、文件事后改变、使用 nominal/sim 值或不确定性低报都会 fail-closed。

自动测试里的合格 fixture 只验证合同实现，明确不构成真实测量、硬件验收、HIL/REAL 或最终发布。

### v71 当前干净 HEAD Combined SIM 机器闭环

当前 parity 下的 `combined-sim-qgc-20260905T175019Z-64536` 已在干净提交
`73429e505d33ca5c645be3d3d0b6cda667f33c5f` 上完成 900 帧真实
PX4/Gazebo/QGC 联合验收：原生 Fly View 按候选框中心点击并收到相关 `SELECT_TRK` ACK，QGC 状态
进入 `TRK`；随后连续发布 832 份测距状态和 832 份目标定位状态，其中 336 帧满足定位质量门，
496 帧明确保持不可定位，operator transport error 为 0。900/900 RTSP frame ACK、signed runtime
type 23 与 QGC post-adoption type 24 完整闭合；VTOL Mission 同时完成 MC→FW→MC→着陆/解锁，
容器及测试端口全部清理。source 明确记录 `dirty=false`，独立 verifier 44/44 通过。

该记录是 software/SIM 机器证据，`operator_visual_acceptance=not_claimed`；它没有关闭人工视觉、
真实 Jetson/相机/Pixhawk、室外 GNSS/RTK、HIL/REAL 或实飞门，最终 `v0.3.0` 仍阻断。

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

### v61 原生 QGC 定位卡片与地图机器捕获

已有当前 parity 的 schema-v2 evidence 时，可将其中最后一帧的融合测距、WGS-84 坐标和 1σ
不确定性通过现有签名 operator link 送入当前原生 EXE，并选择抓取同一 acceptance PID/HWND：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_px4_sitl_qgc_operator_acceptance.ps1 `
  -TargetGeolocationEvidencePath `
    .\artifacts\evaluation\terminal-continuation-v61\gazebo-high-slope-geolocation-evaluation-v61-pre8-v2.json `
  -CaptureNativeVisualEvidence `
  -NativeVisualEvidenceDirectory `
    .\artifacts\evaluation\terminal-continuation-v61\native-visual-manual-review
```

脚本只接受 schema v2、`gate_passed=true`、display-only 且与当前 `release-parity.json` 完全匹配的
定位证据。截图模式会持续刷新 type 10/22、附着 acceptance 已启动的 exact PID、取得物理 client rect、
排除外部遮挡并保存 PNG/observation。v61 机器图已显示完整坐标、1σ、类别、距离、方位、更新时间、
复制/快照/记录按钮和只读边界；它仍是机器证据，`operator_visual_acceptance=not_claimed`，需人工签字后
才能关闭人工视觉门。

### v62 三机型 PX4 实际位移响应矩阵

固定翼、多旋翼和 VTOL 已分别在 disposable PX4/Gazebo Docker 中走同一条
`perception → AI executor → authority/watchdog → MANUAL_CONTROL → PX4` 链路，并只用 PX4
`GLOBAL_POSITION_INT` 计算实际水平位移。三机型最大位移分别为 `33.872547 m`、`1.171039 m`、
`3.784929 m`；均通过目标刷新、heartbeat、POSCTL 连续性、容器安全与清理检查，且未读取 Gazebo
真值或注入 pose。

`scripts/run_px4_reported_position_response_matrix.py` 将三份报告、scenario、当前 release parity 和
三个生成/验证工具源码 SHA-256 汇总；`scripts/verify_px4_reported_position_response_matrix.py` 可独立
重放。该矩阵现已作为 `px4_reported_position_response_matrix` 接入 E7；它只关闭外部 SIM 动态响应
子门，硬件、HIL、REAL、人工视觉和实飞仍为 `not_claimed`。

### v63 三机型响应重复性与故障注入

`run_simulator_docker_ai_acceptance.py` 新增三个显式、仅 disposable loopback PX4 SITL 可用的负向
故障：`target_refresh_dropout`、`heartbeat_dropout` 和 `flight_mode_loss`。每种故障都要求容器/PX4
runtime 正常启动后由对应飞行安全门拒绝，不能靠启动失败制造假阴性。

`run_px4_reported_position_response_campaign.py` 要求固定翼、多旋翼和 VTOL 各至少两次互不相同的
正常响应，并要求每机型覆盖全部三种故障；它输出位移、持续时间、position sample 和目标更新率的
minimum/P50/P95/maximum。独立 verifier 会重开全部 15 份源报告并复算结果。当前 campaign 已作为
`px4_reported_position_response_campaign` 接入 E7；SIM 子门通过不改变硬件、HIL、REAL 或实飞声明。

### v64 原生 QGC 人工视觉验收合同

`multidetect-qgc-visual-review` 把人工点验绑定到 exact PID/HWND 机器报告、原始 PNG、实际 QGC EXE、
当前 `ui_contract_hash` 和近地平线负控。12 项 checklist 必须逐项为 `true`，负控必须至少 5 帧均有
检测、全部测距无效且假坐标为 0；任一文件漂移、遗漏、UI contract 变化或 decision 非 accepted 都
fail-closed。`review_sha256` 是内容完整性摘要，不冒充电子身份签名。

先复制并逐项检查 [`configs/qgc-visual-review-checklist.json`](configs/qgc-visual-review-checklist.json)，
再按 [QGC Windows 发布说明](docs/qgc-windows-release.md)生成记录。人工确认完成并通过独立 verifier
之前，E7 的 `operator_visual_acceptance` 继续保持 `not_claimed`。

### v65 四模式人工视觉证据正式接入 E7

每份人工记录现在必须声明并匹配机器报告中的 `runtime_mode`。E7 writer 接受可重复传入的
`--qgc-operator-visual-acceptance-evidence`，逐份重开所有绑定源并按固定顺序汇总
`REPLAY / SIM / HIL / REAL`；缺一模式、重复 mode、复用同一 machine report 或相同 screenshot、
记录/图片/EXE/parity 漂移都会 fail-closed。最终 release channel 必须取得 4/4 独立通过记录。

当前 v61 exact PID/HWND 截图只证明 HIL 机器渲染，不能复制为其他三个模式。仓库当前没有人工
accepted record，E7 中该集合为 `present=false/passed=false`，所以没有把人工验收状态提前改成通过。

### v66 四模式机器捕获统一契约

机器捕获不再要求 REPLAY/SIM/REAL 伪装成 PX4-HIL 报告。`capture_qgc_native_style.ps1` 现在强制
显式选择 `replay/sim/hil/real`，并在 schema-v2 observation 内绑定实际 EXE、canonical
`release-parity.json`、run ID、exact PID/client rect、遮挡状态和 PNG 身份。示例（先由对应模式的
runner 启动并稳定 QGC，再附着其精确 PID）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\capture_qgc_native_style.ps1 `
  -ExistingProcessId $qgcPid `
  -RuntimeMode replay `
  -RunId replay-visual-001 `
  -ReleaseParityPath .\configs\release-parity.json `
  -OutputDirectory .\artifacts\evaluation\replay-visual-001
```

已有 HIL wrapper 会自动传入 `-RuntimeMode hil`。旧 PX4-HIL acceptance JSON 仍可复验，但只允许归属
HIL；四种模式仍需各自独立机器捕获和人工逐项签字，当前仍为 0/4。

### v67 当前源码 900 帧联合 SIM 机器门

当前源码提交 `005da90` 已在 pinned Docker PX4/Gazebo、同一 portable QGC 和动态 loopback 端口上
完成真实 900 帧联合运行。Gazebo `CameraInfo` 按会话级静态标定处理；NavSat/Odometry 继续执行
1.5 秒动态时效门。4 张因 transport worker 短暂错序而无法安全绑定动态遥测的图像被丢弃，随后用
新鲜样本恢复；最终 image、RTSP、signed type-23 和 QGC post-adoption type-24 均为 900/900。

权威 source evidence：
`artifacts/evaluation/combined-sim-qgc-20260905T142324Z-32372/combined-evidence.json`，255,480 bytes，
SHA-256 `FD386165212A68C6E2D053A0D58B05D796565AB716CB088439F0A6756EDC1090`。独立 verifier 的 41/41
checks 通过，容器和 9 个测试端口均清理。该机器门没有运行目标测距/落图验收，也没有产生人工签字；
四模式视觉、K/P/畸变语义、HIL/REAL、硬件和室外 GNSS 仍待完成。

### v68 CameraInfo 标定语义机器门

Gazebo sensor helper 现在将实际 `CameraInfo` 的 `K[9]`、`P[12]`、`R[9]`、`PLUMB_BOB` 和 `D[5]`
与 [`gazebo-front-camera-v1.json`](simulation/gazebo/calibration/gazebo-front-camera-v1.json) 逐字段绑定；
backend identity 也包含 canonical calibration hash。真实 Gazebo 的 float intrinsic 量化使用明确的
`1e-4 px` 上限，非有限数、结构错误、畸变或超过容差的漂移都会停止运行。

当前权威 source 是
`artifacts/evaluation/combined-sim-qgc-20260905T150510Z-27780/combined-evidence.json`，256,194 bytes，
SHA-256 `E800968AB63BF847CA53BE7798EAD3B2501B731E958D76AB5E8EAC447A999471`。它在干净提交
`c0a4c3256b9d4db45f01174614a672d87c281281` 上完成 900/900 sensor、RTSP、type-23、post-adoption
type-24 和 odometry，CameraInfo 状态为 `validated_k_p_rectification_plumb_bob_distortion`；独立 verifier
41/41 通过。该结果仍不包含人工签字、HIL/REAL、真实硬件、室外 GNSS/RTK 或实飞批准。

### v69 独立 combined 传感器硬门

combined source 与独立 verifier 现在都强制 CameraInfo K/P/R/D 验证状态；独立 verifier 还会重算
sensor expected/received/complete 和逐帧 odometry 绑定，不能只信根级 `gate_passed=true`。当前权威
source 是 `artifacts/evaluation/combined-sim-qgc-20260905T152520Z-75420/combined-evidence.json`，
SHA-256 `9CAB1BC405571C2F287D1545D77B7EA1F7C4A56CC163AB76E1402CB362DB121B`；增强后的独立 verifier
44/44 通过。最终 `v0.3.0` 的人工、HIL/REAL、硬件和室外门没有因此提前关闭。

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
