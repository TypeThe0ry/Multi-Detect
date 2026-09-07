# Implementation roadmap

## typed RuntimeObservationResult 生产入口（v187，`v0.3.0-pre.8`）

- [x] 新增 `multidetect.runtime_observation.RuntimeObservationResult`，从唯一的
  `RuntimeRunReport` 派生 HIL/REAL result 的 event、mode、descriptor/input hash、逐帧身份、数量、
  bounded termination、内嵌 canonical report 和固定 receive-only 输出锁；构造器拒绝歧义终止状态。
- [x] 新增 `write_runtime_observation_pair` / `load_runtime_observation_result` 与独立
  `runtime-observation-result.schema.json`；可选 result `sha256`、严格 JSON 重开、嵌套报告哈希和
  顶层派生字段篡改均有回归覆盖。
- [x] 生成 v187 HIL/REAL 软件 pair、binding 正向重开和 `runtime_input_hash` 篡改负向证据；现有
  `capture_pixhawk_odometry_covariance.py` 的 telemetry request 发送计数不被错误转换为零发送结果。
- [ ] 把该 builder 接入真实 HIL/REAL capture producer：producer 必须明确区分 telemetry request 与
  application/flight-control message counter，并在同一 RuntimeProfile、Mission feedback、endpoint/
  vehicle identity sidecar 下重跑；软件 fixture 不升级硬件、室外 GNSS、人工 Fly View、HIL/REAL 或实飞门。

## HIL/REAL RuntimeReport 观测适配器（v186，`v0.3.0-pre.8`）

- [x] 为 `multidetect.runtime_report_binding` 增加显式 `hil_runtime_observation_finished` /
  `real_runtime_observation_finished` 适配器，统一重开 RuntimeRunReport、descriptor/input hash、逐帧身份、
  bounded termination 和内嵌 canonical report。
- [x] 把 `observation_only`、`read_only`、`receive_only`、零发送计数、飞控/硬件输出关闭和
  `output_locked` 固化为 `output_safety_contract`，同步单绑定/集合 schema；事件/模式漂移、发送计数篡改、
  缺少终止字段和报告不一致均 fail-closed。
- [x] 新增 `scripts/generate_runtime_report_binding_observation_evidence.py`：生成四模式 `4/4` 软件 fixture、
  REPLAY/SIM 子集和发送计数负向报告；正向与负向输出均由同一 schema 重开校验。
- [ ] 将该结果形状接入真实 HIL/REAL capture producer，并用同一版本 RuntimeProfile、PX4 Mission feedback、
  endpoint/vehicle identity sidecar 重跑；合成 fixture 不得升级真实硬件、室外 GNSS、人工 Fly View、HIL/REAL 或实飞门。

## RuntimeProfile → PX4 Mission feedback 六源绑定（v185，`v0.3.0-pre.8`）

- [x] 新增 `multidetect.px4_mission_feedback_profile_binding` 与
  `multidetect-px4-mission-feedback-profile-bind`，显式接收 RuntimeProfile sidecar 集合和既有
  `px4_mission_feedback_binding` 报告；不扫描目录，所有输入与引用源都必须在同一 `base_directory` 内、不可为符号链接。
- [x] 重新打开 v184 四模式 profile policy，逐叶重开六个 HIL/REAL Mission feedback report，核对模式对应
  profile kind、HIL/REAL signer `key_id`、runtime binding gate、receive-only、`flight_control_enabled=false`
  和 `output_locked=true`；profile set 只提供模式策略，六源 reviewer 继续提供每个空军架构的实际身份/哈希。
- [x] 新增 `px4-mission-feedback-profile-binding.schema.json` 与 3 项定向回归（成功、profile 集合不完整、CLI
  错误输出）；输出包含两个输入 source 的 bytes/SHA-256、四个 mode policy、六个 leaf 结果和固定 claim boundary。
- [ ] 现场取得同一版本的真实 HIL/REAL profile、Mission 与 feedback sidecar 后，运行该 joiner 并把成功报告挂入
  发布证据；在硬件、人工 Fly View 与实飞门齐全前，最终 `v0.3.0` 仍保持阻断。

## RuntimeProfile sidecar 集合预检（v184，`v0.3.0-pre.8`）

- [x] 新增 `multidetect.runtime_profile_binding_set` 与
  `multidetect-runtime-profile-set-verify`，显式重开每个模式的 RuntimeProfile JSON，稳定按
  `REPLAY → SIM → HIL → REAL` 排序；HMAC key 只从内存/环境变量读取，不写入证据。
- [x] 集合门检查 required-mode 覆盖、重复 mode/source、模式对应 profile kind、HIL/REAL 签名与 `key_id`、
  endpoint/vehicle/firmware/airframe/signing identity 绑定和全模式锁输出；结构错误、损坏签名和身份漂移均
  fail-closed，输出带独立 schema 与 claim boundary。
- [x] 9 项定向回归覆盖 REPLAY/SIM 子集、默认四模式关闭、合成 HIL/REAL 签名身份、重复/符号链接、篡改、
  身份不匹配、CLI 成功/错误；这只是 software profile preflight，不启动 runtime、不连接 PX4、不改变 E7。
- [ ] 现场采集真实 HIL/REAL profile、Mission feedback 和 endpoint identity 后，再将 profile 集合与六源
  `px4_mission_feedback_binding` 逐叶对账；在真实硬件与人工 Fly View 证据齐全前，最终发布门保持关闭。

## 多模式 RuntimeReportBinding 集合校验（v183，`v0.3.0-pre.8`）

- [x] 新增 `multidetect.runtime_report_binding_set` 与
  `multidetect-runtime-report-set-verify`，显式接收每个 runtime mode 的 result/sidecar 对，按
  `REPLAY → SIM → HIL → REAL` 稳定排序并逐项重开单绑定 verifier；不扫描 `artifacts/`，所有源文件必须
  解析在指定 base directory 内。
- [x] 集合门严格检查 required-mode 覆盖、重复 mode、结果/sidecar/跨类型源复用、声明 mode 一致性、
  14 项单绑定检查和 entry error；缺少 HIL/REAL 时保持 `complete=false` / `gate_passed=false`，不会把
  Replay/SIM 软件记录提升成硬件、HIL、REAL 或实飞验收。
- [x] `runtime-report-binding-set.schema.json` 覆盖成功与结构错误输出；真实 v181 integrated Replay +
  v183 `river_rescue_vtol_001` SIM 2 帧集合证据为 2/2 通过，schema errors 为 `0`，9 项定向回归（8 passed、
  1 个 Windows symlink 权限 skip）、ruff、format、compileall 通过。
- [ ] 仍需在现场 portable 上人工打开 Fly View、载入真实长回放，并在获得 HIL/REAL 结果与 sidecar 后
  扩展集合覆盖；该软件集合门不改变 E7、室外 GNSS、硬件、HIL/REAL 或实飞状态。

## RuntimeReportBinding 失败输出 schema 化（v182，`v0.3.0-pre.8`）

- [x] CLI 异常路径输出稳定 `runtime_report_binding_error` 文档：完整检查键、明确 null 输入区、结构化错误类型/消息、
  reasons 和非零退出码；成功/失败均可由同一 binding schema 解析。
- [x] 损坏 JSON fixture、schema 校验与 44 项定向回归通过；v182 全量为 3,728 项（3,724 passed、4 skipped）。
- [x] 失败输出仍保持 software-only claim boundary，不修改 `px4_mission_path` parity、QGC C++/QML 二进制或
  地图/Mission/飞控/物理释放路径。
- [ ] 继续在现场 portable 上人工打开 Fly View、载入真实长回放，并把成功/失败绑定报告与外部采集、HIL/REAL 记录
  逐帧对账；该门通过前不改变 E7、室外 GNSS、硬件、HIL/REAL 或实飞状态。

## integrated Replay 结果格式兼容与严格帧身份绑定（v181，`v0.3.0-pre.8`）

- [x] `runtime_report_binding` 同时适配当前嵌套 `result` 和历史真实 integrated Replay/QGC 顶层结果字段，保留
  同一 RuntimeRunReport、descriptor/input hash、自然耗尽与无输出边界；不依赖重跑生产 runtime。
- [x] 对 `frame_ids`/`processed_frame_ids` 做严格字符串、非空、唯一性校验；新增旧格式成功绑定与 malformed ID
  fail-closed 回归，使用 v178 真实 sidecar 生成 v181 绑定证据。
- [x] v181 定向 43 项、全量 3,727 项（3,723 passed、4 skipped）通过；该软件消费者不改变 `px4_mission_path`
  parity、QGC C++/QML 二进制、地图/Mission/飞控或物理释放路径。
- [ ] 仍需在现场 portable 上人工打开同一 Fly View、载入真实长回放并将绑定报告与外部采集、HIL/REAL 记录逐帧
  对账；该门通过前不改变 E7、室外 GNSS、硬件、HIL/REAL 或实飞状态。

## 运行结果与 RuntimeRunReport 独立绑定（v180，`v0.3.0-pre.8`）

- [x] 新增 `runtime_report_binding` 只读 verifier 与 `multidetect-runtime-report-verify` CLI，重新打开结果和
  sidecar，严格校验 JSON、报告 canonical SHA-256、事件适配器、模式/descriptor/input identity、帧计数与帧 ID；
- [x] integrated Replay/QGC 要求内嵌报告与 sidecar 完全一致；SIM 绑定有限前缀的
  `stream_exhausted=false` / `frame_limit_reached`，并要求 `truth_free=true`、`closed=true`；未知事件保持 fail-closed；
- [x] `SimulatorScenarioRunResult` 序列化精确 `runtime_input_hash`，实跑 `river_rescue_vtol_001` 2 帧证据完成
  结果↔报告绑定，41 项绑定/场景回归和全量 `3721 passed, 4 skipped, 0 failed`（3,725 tests）通过；
- [ ] 仍需在现场 portable 上人工打开同一 Fly View、载入真实长回放并将绑定报告与外部采集、HIL/REAL 记录逐帧
  对账；该门通过前不改变 E7、室外 GNSS、硬件、HIL/REAL 或实飞状态。绑定器是下游软件证据消费者，不新增
  `px4_mission_path` parity 项，也不触发 QGC C++/QML 重链。

## integrated Replay/QGC RuntimeRunReport parity（v179，`v0.3.0-pre.8`）

- [x] `integrated-replay-qgc` 在本地 RTSP/metadata 同步 ACK 完成后写出 `RuntimeRunReport` sidecar，并将
  同一 canonical mapping 嵌入结果 JSON；
- [x] QGC 回归核对逐帧 ID、descriptor/input hash、自然耗尽、`max_frames=null` 与 loader round-trip，确保
  QGC 入口和 headless Replay 使用同一运行报告契约；
- [ ] 仍需在现场 portable 上人工打开同一 Fly View 并载入真实长回放，将报告与外部采集、HIL/REAL 逐帧对账；
  该门通过前不改变 E7、室外 GNSS、硬件、HIL/REAL 或实飞状态。

## integrated Replay 自然耗尽与 RuntimeRunReport（v178，`v0.3.0-pre.8`）

- [x] 有限 `ReplayFrameSource` 发出可区分的 `FrameSourceExhausted` 结束信号；Live runner 记录
  `camera.stream_exhausted`，不把结束误报为相机故障或成功帧；
- [x] `IntegratedReplaySession` 对账完整 recording 的实际消费帧 ID、数量、descriptor/input hash 与自然耗尽，
  输出与 SIM/HIL/REAL 共用的 hash-bound `RuntimeRunReport`；兼容的 `max_frames == frame_count` 归一化为 `null`；
- [x] `integrated-replay` / `integrated-replay-qgc` 支持 `--runtime-report` sidecar，结果内嵌 canonical report，
  140 项定向回归覆盖 CLI、round-trip、重复运行保护和 source compatibility；
- [ ] 仍需在现场 portable 上人工载入真实长回放，并将报告与外部采集、HIL/REAL 记录逐帧对账；该门通过前不改变
  E7、室外 GNSS、硬件、HIL/REAL 或实飞状态。

## SIM RuntimeRunReport sidecar 与 bounded-prefix 对账（v177，`v0.3.0-pre.8`）

- [x] `SimulatorScenarioRunResult.runtime_run_report` 复用 Replay/HIL/REAL 的 `RuntimeRunReport`，绑定
  manifest runtime channel hash、SIM `RuntimeDescriptor`、共享 ManualClock 和稳定的处理帧 ID；
- [x] 固定 frame budget 消耗仍以场景证据的 `frame_budget_complete` 表示，但 sidecar 明确
  `stream_exhausted=false`、`termination_reason=frame_limit_reached`，避免把有限前缀误报为自然完整流；
- [x] `multidetect.simulator.scenario_runner --runtime-report <path>` 支持原子保存和独立加载复核，补充
  CLI/round-trip 回归；
- [ ] 现场 portable 长回放仍需由人工把 sidecar 与外部采集、HIL/REAL 记录逐帧对账；该门通过前不改变 E7、
  室外 GNSS、硬件、HIL/REAL 或实飞状态。

## 运行时完成报告与确定性前缀阻断（v176，`v0.3.0-pre.8`）

- [x] `AutonomyRuntime.run_with_report()` 记录 `stream_exhausted`、`max_frames`、处理帧数和稳定终止原因；
  被 `max_frames` 截断的有效前缀明确标记为 incomplete，不再被当成完整 Replay/SIM 运行；
- [x] `RuntimeRunReport` 提供严格 JSON schema、canonical JSON/SHA-256、原子写入与加载复核，报告身份继续绑定
  `RuntimeDescriptor` 与 backend input hash；
- [x] `configs/schemas/runtime-run-report.schema.json` 纳入 `px4_mission_path` parity，Python 回归覆盖完整流、
  有界前缀、派生字段篡改和 JSON schema 校验；
- [ ] 仍需在现场 portable 上人工载入真实长回放并将完成报告与外部采集、HIL/REAL 记录对账；该人工/硬件门通过前，
  不改变 E7、室外 GNSS、硬件、HIL/REAL 或实飞状态。

## 全量摘要一键复制与 parity 重绑（v175）

- [x] Fly View 回放弹窗提供完整摘要剪贴板导出，绑定 native 已验证的 bundle/session、质量统计、时长、媒体字节和完整性状态；
- [x] 摘要按钮仅在 `loaded && integrityVerified` 时可用，保持只读剪贴板边界，不进入地图、目标、Mission、飞控或物理释放路径；
- [x] 重链 Release/portable、通过 native self-test 与 offscreen simple boot，完成 45 项回放/QGC 定向回归；
- [x] 以新 UI parity 重建三机型 external/PX4 派生证据并重算 report-only E7；
- [ ] 在现场 portable 上人工载入长回放并核对摘要文本、复制结果和长文本布局；该人工门通过前不改变 E7、室外 GNSS、
  硬件、HIL/REAL 或实飞状态。

## 全量回放质量摘要与 parity 重绑（v174）

- [x] native replay store 与 Python bundle 对完整事件流计算可定位、有效测距、有媒体、唯一目标、捕获时长和媒体字节统计；
- [x] Fly View 筛选显示匹配总数与当前显示数，长回放仍保留最多 512 条 UI 投影且不改变只读边界；
- [x] C++ self-test、Python/native 跨语言加载、QGC 静态契约、portable offscreen boot、定向回归与完整 Python 回归通过；
- [x] 以本轮 parity 重建三机型 external runtime/AI join、PX4 response matrix/campaign，并重算 report-only E7；
- [ ] 在现场 portable 上人工载入长回放，核对全量摘要、筛选匹配数、复制证据和长文本布局；该人工门通过前不改变
  E7、室外 GNSS、硬件、HIL/REAL 或实飞状态。

## 当前 parity 重绑与 E7 report-only（v173）

- [x] 用当前 `release-parity.json` 重建并独立复核三机型 external runtime/AI join；
- [x] 重建并复核 PX4 `GLOBAL_POSITION_INT` 三机型 response matrix 与 repeatability/fault campaign；
- [x] 重算 report-only E7，记录 manifest/report 身份与 32 条详细阻断；
- [ ] 完成现场人工 Fly View 回放审阅和网段/Jetson/GR01/相机真实链路，再决定是否进入 HIL/REAL 门。

## 回放证据明细、筛选与复制（v172）

- [x] native replay store 投影媒体 `kind`、encoding、bytes、尺寸与完整 SHA-256，并严格复核源帧/车辆姿态元数据；
- [x] Fly View 回放弹窗提供全部、可定位、有效测距、有媒体四种只读筛选；
- [x] 每个事件支持复制包含 GPS、测距、车辆与媒体身份的证据文本，不调用地图、任务、飞控或物理释放；
- [x] C++ self-test、Python bundle 跨语言 `--load`、portable offscreen simple boot、45 项定向回归和 3,708 项全量回归通过；
- [ ] 在同一现场机人工打开当前 portable，载入真实 RTSP/GNSS/测距 bundle，核对筛选、复制和长文本布局；该人工门通过前
  不改变 E7、室外 GNSS、硬件、HIL/REAL 或实飞状态；
- [x] v173 已以当前 v172 parity 重新绑定 external/PX4 派生证据并重算 E7 report-only。

## 原生目标事件回放只读查看器（v171）

- [x] `MultiDetectTargetEventReplayStore` 在 C++ 侧一次性加载并校验 `manifest.json`、`events.jsonl` 和 `media/`；
- [x] 拒绝重复 JSON key、未知字段、非有限数、路径逃逸/符号链接、时间/序列/GPS/测距不合格、目录额外文件
  以及 bytes/SHA-256 不匹配，并在任何失败时清空状态；
- [x] QML FileDialog 与只读时间线显示 bundle/session、事件、GPS、距离/方位和媒体引用；
- [x] native self-test 覆盖有效包、媒体篡改与重复 key 负向路径，Python 生成的 bundle 可由 C++ `--load` 跨语言复核；
- [x] portable simple boot 退出 0，回放查看器不调用地图、目标选择、任务、飞控或物理释放；
- [ ] 在同一台现场机上人工打开 Fly View，选择一份真实 RTSP/GNSS/测距录制并核对时间线可读性；该人工门通过前不
  改变 E7、室外 GNSS、硬件、HIL/REAL 或实飞状态。

## Hash-anchored GR01 review trust registry（v150）

- [x] closed registry 登记 operator/reviewer principal、identity、key ID、Ed25519 public key、有效期与吊销元数据；
- [x] registry 文件本身必须匹配包外 SHA-256，review/verify 均重开同一锚定文件；
- [x] operator key 在 source attestation 与 review 时点均有效，reviewer key 在 review 时点有效；
- [x] revoked/expired/mismatched/duplicate key、未来 attestation 与角色复用 fail-closed；
- [x] registry/review schema、实现、wrapper 和负向测试纳入 safety parity；
- [ ] 由组织离线签发真实 registry、独立分发其 SHA-256，并在两台现场机完成真人映射和双签复核。

## Dual-signed GR01 source/review trust chain（v149）

- [x] source operator 使用独立 Ed25519 key 签署 inventory identity，seed 只从环境读取；
- [x] detached attestation 位于 inventoried session 目录外，并绑定 bundle ID、inventory bytes/hash 与 bundle hash；
- [x] 第二机必须使用包外预登记 operator 公钥验证 source attestation，不信任内嵌公钥；
- [x] review schema v4 绑定 source attestation，并强制 operator/reviewer identity、key ID、公钥全部分离；
- [x] wrong key、signature tamper、role/key reuse 均在 transfer/review 输出前 fail-closed；
- [ ] 预登记真实 operator/reviewer 公钥，在真实第二机完成双签验证并归档组织身份映射。

## Reviewer-signed GR01 portable review（v148）

- [x] review receipt v3 使用 Ed25519 签署完整 canonical payload，私钥仅从环境读取；
- [x] receipt 绑定 reviewer、key ID、公钥、signed-payload hash 与 signature；
- [x] 独立 verifier 强制使用包外固定公钥，错误公钥和签名篡改 fail-closed；
- [x] 缺失/非法 signing seed 在任何 transfer/review receipt 写入前零输出；
- [x] claims 区分 key possession 与未验证的真人身份，不扩大硬件/飞行声明；
- [ ] 预登记真实 reviewer 公钥，在第二台现场机器签署并由第三方使用预登记公钥复核真实 session。

## Trusted GR01 transfer-inventory anchor（v147）

- [x] portable review 必须显式接收独立保存的 inventory SHA-256；
- [x] 在任何 transfer/review receipt 写入前核对 inventory 文件哈希，mismatch 零输出并 fail-closed；
- [x] review schema v2 绑定 expected/actual hash 与 match，consumer 重新计算；
- [x] 现场 producer 输出需经独立渠道记录的 inventory SHA-256；
- [ ] 真实现场 session 通过独立渠道传递哈希，并在真实第二台机器执行 `review/verify` 后归档签字。

## Portable GR01 final-session review（v146）

- [x] 现场 final record 强验证成功后自动生成 6 文件 byte-exact transfer inventory；
- [x] 第二机离线重算五源 file identity、portable handoff、GR01 bench、配置与 manifest/report GR01 绑定；
- [x] closed review receipt 明确 target environment/full E7 transitive sources 未重验，保持 no-control/no-flight claims；
- [x] 复制篡改、record 投影篡改、重造 inventory、receipt claim/canonical hash 篡改均 fail-closed；
- [ ] 对真实 GR01 session 包在真实第二台机器运行 `review/verify`，归档 operator/reviewer 签字和独立渠道哈希。

## Imported source-plan pre-network binding（v145）

- [x] 现场 producer 支持 `-SourcePlan`，逐字导入到 session 并核对复制前后 SHA-256；
- [x] source input 必须是 regular non-reparse file，PlanOnly/import 参数冲突在写 artifact 前拒绝；
- [x] ready target handoff 与 CLI endpoint/hardware/CIDR/阈值/secret names/configuration digest 在 UDP 前对账；
- [x] 参数 drift exit 1 且不产生 bench；原参数 positive match exit 0；
- [ ] 在隔离飞机网段导入由独立开发机生成的同 commit plan，使用真实 target secrets 完成五源 session；
- [ ] 对真实 session 包执行离线第二机复核并归档人工签字结果。

## Five-source GR01 final session（v144）

- [x] session schema v3 distinct/hash-bind plan、handoff、bench、E7 manifest 与 E7 report；
- [x] consumer 严格复验目标 handoff/environment，并证明 handoff 指向传入的同一 plan；
- [x] 对账 origin/target Git、release label、safety parity、plan bytes/SHA 与实际 bench/E7 配置；
- [x] producer 在任何 UDP 前创建并严格验证 target handoff，final creator 显式接收 `--handoff`；
- [x] relocated non-ready origin plan + ready target handoff 正例，以及 handoff tamper/config drift/five-source distinct 回归；
- [ ] 在隔离飞机网段使用真实 target host ID/secrets 执行完整五源 session，并由独立机器复核；
- [x] 增加由开发机预生成 source plan 直接导入现场 producer 的安全入口与 pre-network 参数匹配检查。

## Portable GR01 plan deployment handoff（v143）

- [x] source plan 的配置、操作序列、来源身份与 claim 边界可在忽略 origin absolute execution paths 后验证；
- [x] 目标机重算 tracked-clean、Git HEAD、canonical parity、tool identity 与 parity source verification；
- [x] handoff receipt 绑定 target repo/Python/output、host ID、source plan hash 与目标 secret presence，不保存值；
- [x] checkout 路径搬迁、target source mismatch、environment drift、source plan tamper、path escape 与覆盖均有回归；
- [x] source repository/parity/tool identity 深层字段非法时在 receipt 写入前直接拒绝；
- [x] clean commit 上完成 no-network diagnostic smoke，source equivalent 但缺 secret，因此严格门保持关闭；
- [ ] 把 source plan 与目标机 receipt 一起交给现场机，注入真实 secret 后生成 ready receipt，再执行真实 session；
- [x] 通过的 handoff receipt 已纳入 final session 五源绑定，避免现场执行记录与交接记录分叉。

## Execution-plan-bound GR01 final session（v142）

- [x] session record v2 hash-bind source-bound plan，并投影 Git/parity/ready 身份；
- [x] consumer 重跑 plan verifier，再核对 plan 与 bench/E7 endpoint、身份、CIDR、阈值及 freshness；
- [x] producer 在最终 record create 时显式传入 bench 前通过的同一 plan；
- [x] plan source/config tamper、缺 plan 与四源非唯一性均 fail-closed；
- [ ] 在真实隔离网段执行 ready plan → GR01 bench → E7 → final session，并离线复核四份来源。

## Source-bound GR01 field-session plan（v141）

- [x] plan v2 绑定 Git HEAD、tracked-clean、canonical parity 来源验证及四个执行工具身份；
- [x] 独立 verifier 重开 parity/tools，并支持复核 secret presence 和要求 ready；
- [x] 实际 session 在任何 UDP 前强制 create + `--check-environment --require-ready`；
- [x] dirty tree、stale parity、tool tamper、environment drift、missing secret 与 non-ready 均有 fail-closed 回归；
- [x] 干净提交下完成无网络 smoke；来源通过但 secret 缺失，故仍为 diagnostic/non-ready；
- [ ] 在隔离飞机网段的同一干净部署提交中注入现场 secret，先保存 ready plan，再执行真实 GR01 session。

## GR01 field-session no-network rehearsal（v140）

- [x] `-PlanOnly` 在任何 bench/network 操作前验证 endpoint、CIDR、身份、阈值、路径和 Python CLI；
- [x] secret 只记录环境变量名与 present 布尔值，不读取或持久化内容；
- [x] closed plan receipt 固定执行顺序与 no-network/no-hardware/no-control claims；
- [x] 缺 secret 时输出可审计的 `ready_to_execute=false` 回执，不创建伪 session；
- [x] repository-source CLI shim 消除 editable-install 前置条件并进入 safety parity；
- [ ] 在现场机器先保存 plan receipt，补齐 secret 后执行真实 session，并在独立机器对比 deployment identity。

## Transactional GR01 bench-to-E7 session（v139）

- [x] 用单个 PowerShell producer 串联 hardware bench、strict bench verify、E7 manifest/report 与独立 session verify；
- [x] 所有现场身份和质量阈值显式传递，HMAC/MAVLink signing secret 仅按环境变量名称引用；
- [x] consumer 重开三份来源，重跑 GR01 verifier 与 canonical E7 evaluator，并核对 SHA/bytes/freshness/projection；
- [x] closed schema 固定 no-flight/no-payload claims，成功 record write-new 且只在全部步骤通过后生成；
- [x] producer/consumer/schema/entry point 纳入 `safety_policy` parity，并完成 tamper/expiry/parser 回归；
- [ ] 在隔离飞机网段配置真实 secret 与 GR01/G20 endpoint，执行该 session 命令并由另一台机器复核记录。

## GR01 operator-link freshness and deployment identity（v138）

- [x] manifest 原子绑定 GR01 bench 的 UTC 字符串、epoch 与最大允许年龄，默认 900 秒；
- [x] writer 在落盘前拒绝 stale、naive、非有限及超出 5 秒容差的未来报告；
- [x] evaluator 以 canonical `evaluated_at_utc` 重算 age，并复核 source timestamp 与 manifest 投影一致；
- [x] final release 拒绝缺少任一 freshness 字段的历史 GR01 记录，同时保留 prerelease 读取兼容；
- [x] schema、CLI、PowerShell wrapper 和 stale/future/partial/legacy-final 回归闭合；
- [ ] 在真实飞机网段生成带真实 GR01/G20 hardware ID、endpoint/source identity 的新鲜 bench，并在 900 秒窗口内
  创建和评估最终候选 manifest。

## GR01 operator-link final release binding（v137）

- [x] release manifest 写入独立 `real_gr01_operator_link` 并绑定 strict bench 文件身份；
- [x] evaluator 重开源报告、重跑 verifier、逐字段复核 endpoint/source/计数/时延/签名/no-control；
- [x] final release 将 GR01 selection/ACK 与 fire-alert ACK 设为两个不可互相替代的门；
- [x] Windows current-gate wrapper 支持显式 `-RealGr01OperatorLinkBench`；
- [x] schema、CLI、tamper/software-baseline/final-channel 回归及 current-parity E7 复算通过；
- [ ] 在飞机网段以真实 GR01/G20、真实 source route 和签名配置执行 bench，并将通过报告绑定到 E7。

## Clean-checkout manual validation recovery（v131）

- [x] 用独立 detached worktree 复现 Windows 新检出 CRLF 导致的 Ruff format failure；
- [x] 通过 `.gitattributes` 固定 Python LF，并在第二个全新检出验证 1,524/1,524 format/lint/compileall；
- [x] manual workflow 安装 vision/Pixhawk/security/model-tools 测试 extras，dev extra/lock 补齐 requests；
- [x] Git 外本地 evidence/model 集成测试在资产缺失时显式 skip，资产存在时继续执行原严格断言；
- [x] 消除 VTOL capture ready 序列化时间与内部比较值的小于 1 微秒舍入差异；
- [x] Windows Python 3.13.7 隔离干净检出完成 3,574 tests：3,559 pass / 15 explicit skip；
- [x] Windows Python 3.12 主工作树保留本地资产完成 3,574 tests：3,572 pass / 2 platform skip；
- [ ] 从 GitHub Actions 手动触发 Python 3.11/3.12/3.13 hosted matrix，并保存 run URL/ID/commit。

## Ruff format baseline migration（v130）

- [x] Ruff 0.16.4 盘点并格式化 433 个候选，形成 282 个 canonical Python 文件差异；
- [x] 全仓 1,523 个 Python 文件通过 format check，lint、compileall、diff check 同时通过；
- [x] 重建四组受影响 release parity 与三机型外部 join/matrix/campaign 派生证据；
- [x] 重跑 E7 并确认 32 个真实 blocker 保持 fail-closed；
- [ ] 保留 CI manual workflow 实际运行确认；本地闭环不冒充 GitHub-hosted 三版本 Python matrix。

## Deployment-bound SSH host key（v129）

- [x] `ssh -G` 解析目标，`ssh-keygen -F` 从显式 known_hosts 提取匹配 key；
- [x] scp/ssh 只使用每次部署专用 host-key snapshot，排除 global known_hosts，并做前后 hash 检查；
- [x] installation receipt v3 绑定 resolved host、snapshot bytes/SHA 与 canonical fingerprints；
- [x] 独立 verifier 支持明文/hashed host，拒绝篡改、错主机、重复 fingerprint、revoked/未知 key；
- [ ] 通过 Jetson 本机可信控制台核对 fingerprint、写入 known_hosts 后执行真实远端安装并归档四件套。

## Jetson explicit SSH identity（v128）

- [x] 显式 remote user/SSH port，scp 与 ssh 共用相同 destination；
- [x] BatchMode + strict known-host checking + 禁止 UpdateHostKeys，身份或认证异常 fail-closed；
- [x] installation receipt v2/Schema/verifier 绑定 user/port，并覆盖非法用户名与端口负测；
- [x] 真实 package-only 打包/独立复核/无 install receipt 烟测；
- [ ] 由操作员通过可信通道核对 Jetson host-key fingerprint，预置 known_hosts/SSH key 后执行真实部署。

## Jetson remote installation receipt（v127）

- [x] SSH stdout/stderr 持久化，并以退出码 0、唯一 exact READY marker、唯一 RUN marker 共同授权回执；
- [x] 安装回执绑定 package receipt、tar、source HEAD、目标目录、runtime Python、remote output 和远端检查；
- [x] closed Schema、installed CLI、clean-checkout wrapper 与独立重开验证；
- [x] package-only、篡改、重复/缺失 marker、失败检查和越界声明负测；
- [ ] 恢复飞机网段后执行真实安装，保存 package/remote-output/install 三件套并运行独立 verifier。

## 独立 Jetson evidence package verifier（v126）

- [x] receipt closed schema、archive identity、source manifest 与逐文件流式 SHA 复核；
- [x] path traversal、case collision、link/special entry、增删/篡改和安全声明提升负测；
- [x] installed CLI、clean-checkout wrapper、正式 v125 package 独立复验与全量回归；
- [ ] 飞机网段恢复后对上传前包再次运行 verifier，并将结果与远端安装回执共同归档。

## 隔离 Jetson evidence tool deployment（v125）

- [x] 完整 Python source + product version + recorder 的 hash manifest/package/receipt；
- [x] tar path、解包逐文件 SHA、闭合文件集、PowerShell/Bash/import 自复核；
- [x] 独立版本目录原子落位，不覆盖、不 sudo、不重启生产服务；支持离线 `-PackageOnly`；
- [ ] 控制机接入 `192.168.144.0/24` 后上传正式包并保存远端 `JETSON_EVIDENCE_TOOLS_READY` 回执。

## Jetson 一键 RTSP evidence session（v124）

- [x] 单入口串联 RTSP v3 录像、严格 raw replay 转换与 source transfer inventory；
- [x] 独立会话目录、不可覆盖、失败保留部分证据、环境变量凭据隔离与 Bash 语法回归；
- [x] 帧抽样/上限可配置，成功输出机器可解析的 session/inventory 路径；
- [ ] 在真实 Jetson/相机运行默认 300 秒会话，复制到 Windows 并完成 v123 目的端验证。

## Jetson→Windows 证据包传输回执（v123）

- [x] 源端闭合 inventory 绑定逐文件 portable path、bytes、SHA-256 与 canonical bundle hash；
- [x] 目的端重新枚举并保存第二份完整清单，严格拒绝文件增删、变化、symlink 和路径冲突；
- [x] inventory/verification 双 Schema、strict verifier、installed CLI、clean-checkout wrapper 与全量回归；
- [ ] 在真实 Jetson H.265 v3 采集目录执行 snapshot，复制到 Windows 后生成真实 verification receipt 并人工签收。

## 可搬迁 RTSP evidence chain（v122）

- [x] 同目录 RTSP 容器使用 manifest-relative 路径，迁移不改 canonical 内容；
- [x] raw replay source binding 明确记录 `recording_root|absolute` 解析基准；
- [x] 整个父目录迁移后重开 source manifest/video/summary/raw recording 全链；
- [x] 迁移正例、summary 篡改负例和全量回归；
- [ ] 用 v123 工具搬运一份真实 Jetson H.265 v3 证据目录并人工验收回执。

## RTSP evidence container → raw replay（v121）

- [x] RTSP 清单 v3 同时绑定系统 UTC 与单调采集时间窗、视频 bytes/SHA-256、全帧 probe 和 canonical hash；
- [x] URI/凭据不落盘，stream-copy/EOS/无飞控/无物理 release 声明 fail-closed；
- [x] 单命令从已验证 RTSP 清单自动取视频身份与 UTC，原子生成不可覆盖 camera-only raw replay；
- [x] 解码前后源身份复核，输出后重开 source/summary/raw manifest/events/images 全链；
- [x] tracking-review 支持 v3 严格入口并保留 v2 历史读取；真实 OpenCV 容器和篡改负例回归完成；
- [ ] 在 Jetson 对真实 H.265 RTSP 录像运行 v3 采集/转换，并由操作员补人员、设备、地点和分层证明；
- [ ] 为同一时轴连接独立合格 GNSS/raw telemetry；本功能本身不产生定位或 release claim。

## 现场证明时间自动交接（v120）

- [x] video→raw replay summary 输出结构化证明时间窗与待确认 provenance 字段；
- [x] CLI 从已验证 recording 自动派生 start/minimum end，避免人工复制时间；
- [x] 自动推导不自动创建证明，人员、设备、地点、来源和分层仍显式必填；
- [x] `1970-01-01` camera-only 占位锚不可进入 deployment attestation；
- [ ] 用真实 RTSP 容器与现场 UTC 执行转换、证明、intake 的人工验收。

## deployment recording 来源证明门（v119）

- [x] 每个物理现场 raw recording 使用 closed schema 记录采集人、设备、地点、local/RTSP 来源与 UTC 时间窗；
- [x] 证明绑定 recording manifest 的 bytes/SHA-256/recording hash，并要求时间窗覆盖最后事件；
- [x] intake 与 campaign 在标注前重开六份证明，错分层、缺失、篡改或来源漂移 fail-closed；
- [x] final replay manifest 将证明作为 artifact reference 纳入 canonical identity，旧无证明 deployment 清单拒绝；
- [x] installed CLI、clean-checkout wrapper、schema、正负向/篡改/全链回归完成；
- [ ] 现场人员使用真实设备生成六份证明并完成 intake；人为证明仍需配合独立审核，不能替代硬件签名。

## campaign 绑定标注工作台（v118）

- [x] `annotation-ui` 只接受 fresh `annotation_required` campaign；
- [x] status 输出当前 plan 解析出的可执行 UI argv；
- [x] 启动日志绑定 campaign、dataset、plan canonical hash 与 stage；
- [x] intake-ready、reviewed/finalize-ready 和证据漂移 fail-closed；
- [x] 真实子进程随机 loopback HTTP 生命周期和全量回归；
- [ ] 在真实六分层 campaign 使用该入口完成人工标注与独立复核。

## 本机可视化回放标注工作台（v117）

- [x] hash-verified 图像读取与精确 frame ID API；
- [x] canvas bbox、多类别、正/负例、前后导航和计数；
- [x] 独立 reviewer 提交并复用 v116 review-content binding；
- [x] loopback-only、mutation token、CSP nonce、严格 JSON 与路径边界；
- [x] installed/wrapper CLI、HTTP 生命周期、Node 语法和全量回归；
- [ ] 由真实标注员/复核员完成现场视觉与交互验收及六分层 campaign。

## 人工标注 review-content 绑定（v116）

- [x] 逐帧 positive/negative、多目标 bbox 和 annotator CLI；
- [x] 独立 reviewer/UTC review CLI 与下一未标帧提示；
- [x] workspace schema v2 将 review 绑定到全部标注内容；
- [x] 改标撤销 review，外部篡改、并发改写、换 reviewer 均 fail-closed；
- [x] legacy v1 可读，campaign 禁止 v2→v1 降级绕过；
- [x] installed/wrapper CLI、schema、campaign、原子写入和端到端回归；
- [ ] 在真实六分层 campaign 由两名独立人员实际执行 annotate/review。

## 现场六分层回放 campaign（v115）

- [x] 不可变计划绑定六层来源、固定模型、阈值和 canonical hash；
- [x] `create/status/advance` 可恢复推进所有确定性机器步骤；
- [x] 缺现场数据或人工动作时给出唯一 stage/next action，不伪造完成；
- [x] 标注、标注复核、accuracy、threshold 四个身份强制互异；
- [x] 每次重开模型、receipt、accuracy、calibration 和 fresh verification；
- [x] closed schema、wrapper、真实 ONNX Runtime 端到端与篡改回归；
- [ ] 采集真实 day/night/haze/reflection/smoke-only/no-fire recording；
- [ ] 四名独立人员完成标注和两级评审，以真实模型生成 promotion-ready 证据。

## 最终 calibration promotion gate（v114）

- [x] E7 汇总 report/recomputed promotion readiness；
- [x] prerelease 保持 pending calibration 诊断兼容；
- [x] final release 同时要求 verification、provenance 与 promotion-ready；
- [x] 覆盖未 reviewed calibration 的 prerelease/readiness/final-channel 分层回归；
- [ ] 现场数据完成独立 accuracy 与 threshold review，生成两项都为 true 的正式证据。

## calibration provenance 进入 E7（v113）

- [x] verifier 分别投影 report 与 recomputed provenance completeness；
- [x] release manifest hash-bind 两项 provenance，E7 每次重开全链并汇总；
- [x] prerelease 将旧 report/verification/E7 缺失字段规范化为 false；
- [x] final release 要求 calibration verification 通过且 provenance 完整；
- [x] synthetic HIL ONNX receipt→accuracy→calibration→E7 正向链和历史兼容回归；
- [ ] 用真实 field-reviewed bundle 关闭该门，并保持独立 accuracy/model approval 审查。

## calibration 全链 provenance（v112）

- [x] evaluator 与 calibration 共用完整 prediction-run verifier/binding；
- [x] prediction binding 分离 runtime inference、production approval 与 synthetic HIL；
- [x] calibration 成对绑定 prediction receipt 和 fresh-recomputed reviewed accuracy；
- [x] report/schema 固定 receipt、accuracy、manifest/canonical、model artifact hashes；
- [x] verifier 从报告或 override 路径重开全链并拒绝 accuracy 替换；
- [x] wrapper、安装 CLI 与 synthetic HIL ONNX 端到端回归；
- [ ] 在真实 field bundle 上生成 `provenance_complete=true` 的 calibration 和 verification。

## prediction receipt 到 accuracy/readiness 绑定（v110）

- [x] evaluator 接受并独立复验 `prediction-run.receipt.json`；
- [x] 从 verified receipt 派生模型 artifact SHA、ID、版本，拒绝调用者冲突元数据；
- [x] accuracy report 绑定 receipt byte/canonical hash、model manifest、runtime 与 synthetic HIL 事实；
- [x] readiness fresh recomputation 再次复验 receipt 并比较完整 prediction binding；
- [x] wrapper、安装主 CLI、closed schema、synthetic HIL ONNX 端到端及负向回归已完成；
- [ ] 在真实 field-reviewed bundle 上生成 receipt、accuracy、readiness，并完成独立模型评审。

## 固定模型 replay predictions 与 provenance（v109）

- [x] 从 v108 reviewed bundle 复验 annotation/raw/truth 绑定后再启动推理；
- [x] 复用正式 frame decoder、ONNX/TensorRT detector、letterbox/NMS/adapter 与 prediction writer；
- [x] 固定 model artifact/manifest/class/input/output/threshold/provider/runtime 身份；
- [x] 六 strata prediction stream 逐文件 hash、逐帧顺序和延迟统计；
- [x] 原子、不可覆盖输出及 annotation/model/prediction/new-manifest 全链独立 verifier；
- [x] CLI、wrapper、closed receipt schema、test-double 负声明和真实 ONNX Runtime smoke；
- [ ] 在真实 v108 field-reviewed bundle 上运行固定候选模型并保存 receipt；
- [x] evaluator/readiness 已可验证并绑定 prediction receipt；
- [ ] 在真实 field 数据上执行 accuracy/threshold calibration，并由独立 reviewer 评审模型状态。

## replay-set 独立人工标注工作区（v108）

- [x] 从 verified intake 推导逐帧、逐图像 immutable assignment；
- [x] 默认 `unannotated + objects=null`，禁止漏标自动降级为 negative；
- [x] positive/negative 显式 disposition 与 class/bbox 强校验；
- [x] annotator/reviewer 身份分离、UTC review timestamp 和全帧完成门；
- [x] 原子导出六份 truth、reviewed workspace、review receipt 与 replay manifest；
- [x] 独立 verify 重开并对账 raw/intake/workspace/truth/manifest 全链；
- [x] create/inspect/finalize/verify CLI、installed command、closed schemas 与回归；
- [ ] 使用真实 v107 field intake 完成人工双人标注和 review receipt；
- [x] 固定候选模型全帧 predictions 的共享 runner/provenance/verifier 已实现；
- [ ] 在真实 field-reviewed 数据上执行 runner，再做 evaluator/readiness/threshold 独立评审。

## 部署 replay-set 不可变 intake（v107）

- [x] 六个 canonical strata 使用显式 `STRATUM=SOURCE` 绑定，拒绝缺失、额外和重复源；
- [x] 重开并验证 raw recording，只复制 manifest、events 和引用图像；
- [x] sibling temporary 内完成逐文件 hash、recording identity、frame count 和 inventory 回验后 rename；
- [x] 拒绝覆盖、symlink、路径逃逸、重复 JSON key、非有限数和 receipt/artifact/claim 篡改；
- [x] receipt 固定 annotation/prediction/review/accuracy/release 为 pending/false；
- [x] 提供 installed CLI、clean-checkout wrapper、closed schema、独立 verify 与回归；
- [ ] 现场采集六份不同 deployment recording，执行 intake 并保存真实 receipt；
- [ ] 独立完成 ground truth 与固定模型 predictions，再生成 manifest/accuracy/readiness 证据。

## E7 replay-set 可执行 readiness 诊断（v106）

- [x] E7 每次从绑定 manifest 和 accuracy report 重算 readiness，不信任静态声明；
- [x] 输出逐 entry/stratum 来源、标注、真值、预测、hash binding 与下一步动作；
- [x] pending 数据保持 fail-closed，篡改或无法重算继续成为证据错误；
- [x] readiness 只作诊断，不破坏独立 threshold-calibration 的合法替代路径；
- [x] 当前 SIM fixture 明确识别为 6/6 synthetic、6/6 未复核、五个正向 strata 无正例；
- [ ] 采集真实 deployment recordings，完成独立标注复核并运行固定模型生成每帧 predictions；
- [ ] 每个正向 stratum 至少有真实正真值和模型正预测，再进行独立 accuracy/threshold 评审。

## 当前 E7 默认证据链恢复（v105）

- [x] 将 combined SIM/QGC 默认源切换到具备 CameraInfo K/P/R/D 与完整 odometry 的 900 帧记录；
- [x] 为该 combined source 生成当前 release-mode envelope；
- [x] 从六份不可变 source 重算 external runtime/AI join 与 source revalidation；
- [x] 从三机型正向、重复与故障源重算 reported-position matrix/campaign；
- [x] 按当前 Gazebo model/root 字节重建三机型 deterministic 3-D control evidence；
- [x] wrapper 默认执行可生成 report-only E7 输出，不再因旧 parity/model hash 中断；
- [x] 回归锁定固定路径、排除 v56/v65/旧 combined 默认，禁止按 mtime 或通配符漂移；
- [ ] 现场重新采集 ready field-link、四模式人工视觉/Mission、HIL/REAL 和最终版本证据，关闭 32 blockers。

## E7 REAL field-link 地址来源投影（v104）

- [x] writer 从 canonical verifier 结果投影 `address_state_bound`，禁止调用方自报；
- [x] evaluator 重开父预检和嵌套回执，并对账实际 binding presence；
- [x] manifest 字段篡改或嵌套地址源漂移均使 E7 fail-closed；
- [x] 最终 release 要求显式投影，历史 prerelease 保持可读；
- [x] false 保留为合法可审计状态，不把本仓库 helper 强制为唯一现场配置机制；
- [ ] 现场 bound ready preflight 进入 E7 后确认投影为 true，并保存最终审核截图/报告。

## 地址 apply 回执与外场预检绑定（v103）

- [x] 预检 CLI/PowerShell wrapper 接受成功 apply 地址回执并独立复验；
- [x] 将回执路径、字节数、SHA-256、schema、接口、地址/prefix 与 apply 真值闭合绑定；
- [x] 以父预检观测时间验证最多 300 秒新鲜度，使归档验证保持确定性；
- [x] 拒绝回执漂移、binding 篡改、软链接、自引用、what-if、时限放宽、CIDR 越界和 alias 分叉；
- [x] 保持地址回执与 endpoint readiness 语义分离，不能单独提升 hardware gate；
- [ ] 现场真实 apply 后立即生成 bound ready preflight，并由另一进程重开两份证据复验。

## 外场地址证据 closed schema 与独立复验（v102）

- [x] 为 plan/verify/what-if/apply 输出建立统一 closed JSON Schema；
- [x] 独立 verifier 拒绝重复 key、非有限值、未知字段、非法地址/路由和地址重复；
- [x] 从保存状态重算 plan change/elevation、安全不变量和 rollback 一致性；
- [x] 从保存地址重算 apply postcondition，what-if/plan 不能满足 `--require-applied`；
- [x] 支持目标接口/IP/prefix 与最大时效约束；
- [x] producer/schema/verifier/CLI 纳入统一 `safety_policy` parity；
- [ ] 现场真实 apply 后在另一进程使用 `--require-applied --max-age-s 300` 独立复验。

## 地址变更后置条件与 WhatIf 真值（v101）

- [x] `changed` 只反映实际执行的 add/remove，不再复用预先计算的意图；
- [x] 非管理员 `-WhatIf` 可生成审计结果且不触发 UAC；
- [x] `-WhatIf` 明确标记未执行 postcondition，避免把模拟结果冒充 apply；
- [x] add 后要求唯一目标地址与 `SkipAsSource=true`，remove 后要求目标地址不存在；
- [x] 后置条件结果进入落盘 JSON，失败时命令 fail-closed；
- [ ] 现场由操作员确认网线/设备后运行真实 apply，并以 ready preflight 完成端到端复验。

## 可审计的外场网络准备（v100）

- [x] 非管理员先用 `-PlanOnly` 生成无副作用的精确变更计划；
- [x] 计划明确 secondary-only、`SkipAsSource`，且不改默认网关、DNS 或显式路由；
- [x] `-PlanOnly` / `-VerifyOnly` 支持 UTF-8 write-new JSON 证据并拒绝覆盖；
- [x] 提权 child 精确继承输出路径，实际 apply/remove 保持显式提权；
- [x] 将全部 field-link 权威纳入 `safety_policy` release parity；
- [x] 当前桌面只读计划、verify 和 3-sample preflight 均完成，未错误提升硬件声明；
- [ ] 操作员在现场确认网线与设备上电后执行提权 apply，再重新采集 ready field-link 证据。

## 可验证、可回滚的 index 发布（v99）

- [x] index write-new 后立即从磁盘执行完整 verifier；
- [x] 用同一 indexed timestamp 重建 post-write 快照，并与 pre-write 快照逐字核对；
- [x] 跨 build/write 窗口的文件篡改、新增 artifact 或 campaign 漂移均使发布失败；
- [x] 失败只回滚本轮新建输出，不覆盖或删除预存 index/journal 证据；
- [x] 覆盖写后篡改与写后新增 probe 的可重复回归；
- [ ] 在现场采集进程并发完成时反复发布 index，验证只产生完整稳定快照。

## 稳定 journal 快照与 blocked probe 封账（v98）

- [x] 用逐文件 blocked probe 记录替代不可独立复验的裸计数；
- [x] 对每个 blocked probe 重跑 archived verifier，并要求其明确失败；
- [x] index verifier 重开文件并复核 SHA、evidence digest、时间、模式、机型与原因；
- [x] index 构建前后要求 capture quiescent，并双读 campaign 与 session/probe 文件内容快照；
- [x] 覆盖 active capture、passing-or-invalid unreferenced probe、文件篡改及构建中漂移；
- [ ] 在现场并发运行 capture 与 journal-status，确认活动窗口拒绝、事务结束后稳定 index 可复验。

## 恢复租约与成功 session 对账（v97）

- [x] 支持 session write-new 后、正常 lease release 前崩溃的 recovery→index 闭环；
- [x] 每个成功 session 强制 released/recovered 两类 archive 恰好存在一个；
- [x] recovered archive 重开 recovery 文档并重算 digest、lease 文件 SHA、lease/receipt 绑定；
- [x] index closed schema 记录 archive kind 及可审计 lease/recovery path/hash，并约束两类字段组合；
- [x] 覆盖双归档、无归档、缺失 recovery、直接篡改和重新计算 digest 后的错绑攻击；
- [ ] 在现场机器强杀真实 capture 进程，按 death proof 恢复并由第二人复验 session/index。

## 断电安全 lease 发布/释放（v96）

- [x] 在非阻断 candidate 目录完成 lease write-new/fsync，再原子发布完整 active 目录；
- [x] active release 用目录 rename 一步解除阻断，并保留 released lease 审计源；
- [x] candidate 残留不阻断采集且可被 status 计数；managed lifecycle 目录拒绝 symlink；
- [x] interrupted recovery 仅允许相同 lease ID/reason 续跑，不清除 staged recovery；
- [x] journal index 重开每个 released lease 并与 session 嵌入内容逐字核对；
- [ ] 在现场机器以真实进程强制终止/断电演练 acquire、release、recovery 三个边界。

## 独占 covariance capture journal（v95）

- [x] 用 receipt/workspace digest 派生的原子目录 lease 串行化 probe、capture 与 session seal；
- [x] 第二进程在 capture 前拒绝；正常完成仅删除完全未变的自有 lease；
- [x] Windows 用只读进程句柄判断 owner 状态，不用信号探测或静默清理；
- [x] 只有精确 lease ID 且同机 PID 明确死亡时允许恢复，并保留 lease/recovery 双文档审计链；
- [x] session 嵌入完整 lease 并复验 receipt、campaign、leaf 与 digest；
- [x] journal index 对账 `1..N` 连续 session 前缀、当前 verified reports 与 blocked probe 数量；
- [ ] 在真实第二台现场机执行并发抢锁、异常断电恢复和六叶 journal/index 人工复验。

## Receipt-bound 单叶 covariance session（v94）

- [x] 从 v93 receipt 重验本机 workspace/parity，并锁定精确唯一 next leaf；
- [x] online probe 在 capture subprocess 前 write-new 落盘并从磁盘独立复验；
- [x] blocked probe 保留诊断但禁止 capture 和成功 session record；
- [x] 成功后重开 covariance report，生成 closed-schema、digest-bound 的一叶 session record；
- [x] 支持后续叶推进后继续复验旧 record；归档模式只放松 next-leaf 条件，不放松其他在线合同；
- [x] 修复接收后合法 campaign report 与静态 20 文件闭合集的生命周期冲突；
- [ ] 在真实 Pixhawk 上按 receipt 运行六次 run-next，并由第二进程/第二人复验六份 session record。

## 事务式接收 covariance workspace（v93）

- [x] 接收前完整验证源介质，只复制 manifest 精确声明的 20 个文件；
- [x] 在目标同盘临时目录复验 workspace/parity/preflight，成功后才原子落位；
- [x] 拒绝覆盖、bundle 内目标/回执和 symlink，回执写失败时回滚本次新副本；
- [x] 在 bundle 外生成 closed-schema、digest-bound 本机回执，并支持拔除源介质后独立复验；
- [x] 回执所有 acceptance claims 固定为 false，并给出本机 preflight argv；
- [ ] 在第二台现场机从真实移动介质运行 receive/verify-receipt，再逐叶在线采集。

## 可搬迁 covariance campaign workspace（v92）

- [x] 导出精确六叶的 18 份 Mission/feedback/profile 原始字节，并生成相对路径 portable campaign；
- [x] 闭合 manifest 绑定文件全集、逐文件 hash/size、endpoint/baud、源 campaign、当前 PX4 parity；
- [x] 原子发布前重验导出目录，第二根目录删除原副本后再次运行现有六叶 preflight；
- [x] 拒绝路径逃逸、非规范路径、Windows ADS、symlink、额外文件、内容与 parity 漂移；
- [x] 不携带或迁移 online probe/covariance report；接收机必须重新在线确认当前 Pixhawk；
- [ ] 用真实六叶输入导出物理介质包，在第二台现场机运行 verify、preflight 和逐叶采集。

## 可独立复验的 online covariance preflight（v91）

- [x] 将下一叶在线探测固化为闭合 schema-v2 observation，加入 UTC 与 canonical evidence digest；
- [x] 绑定当前 campaign、六叶 source gate、Mission/feedback/profile、schema/runner/builder 与 release parity；
- [x] `verify-probe` 重开来源并重算 mode binding、采样时序、identity query 预算、reasons 与 gate；
- [x] 默认 300 秒过期，并拒绝未来时间、source/parity 漂移、leaf 前移、未知字段和 symlink；
- [ ] 在现场准备真实 campaign 后生成并立即复验每个下一叶 observation，再执行对应 `capture-next`；
- [x] 以 v92 静态 workspace bundle 解决第二机文件路径搬迁；在线 observation 保持绑定本机本次连接，
  不跨机迁移或复用。

## 六叶 covariance source preflight（v89）

- [x] 无 transport I/O 地重开全部 Mission、feedback v2 与 signed runtime profile；
- [x] 逐叶核对三机型 adapter、HIL/REAL mode、configured endpoint 与 baud；
- [x] 强制 feedback/runtime/profile gate、receive-only、zero-transmit、flight-control disabled、output locked；
- [x] 输出固定顺序 READY/BLOCKED 清单，并在 `capture-next` 启动 subprocess 前硬阻断；
- [x] 覆盖 source 漂移、八类语义错配、CLI 失败码与零 subprocess 调用；
- [x] 增加 bounded online `probe-next`，在不生成正式 covariance report 的情况下核对当前 Pixhawk
  HEARTBEAT、firmware identity、resolved endpoint 和 current-link qualification；
- [ ] 在真实三机型端点上运行 `preflight -> probe-next -> capture-next`，保存六叶现场报告并由第二机复验。

## 六叶 covariance 现场采集 runner（v88）

- [x] 严格配置固定 HIL/REAL × fixed-wing/multirotor/VTOL，预解析全部 source/output；
- [x] status 每次重验已存在叶并给出唯一 next leaf；
- [x] capture-next 一次只处理一个叶，支持 dry-run 且拒绝覆盖失败报告；
- [x] 进程返回与落盘 verifier 双重判定，完整后复用 E7 builder；
- [x] 输出六组 E7 参数和可选 matrix binding，更新 Schema、parity、测试与文档；
- [ ] 准备真实六份 Mission feedback/profile，按 runner 指引连接三机型并完成现场采集。

## E7 HIL/REAL estimator covariance 六叶矩阵（v87）

- [x] 定义 HIL/REAL × fixed-wing/multirotor/VTOL 精确覆盖与互异 source 约束；
- [x] 每叶重放 schema-v3 verifier，并绑定 Mission/feedback/profile/link/firmware；
- [x] 接入 release manifest schema、writer API/CLI、evaluator 与 final-release blocker；
- [x] 与四模式 `px4_mission_path_hash` 交叉绑定，覆盖重复、篡改与 source 漂移；
- [ ] 在真实三机型 Pixhawk 上分别采集 HIL/REAL 六份通过报告，并由第二机重放后写入 E7。

## 杆臂几何不确定性传播（v78）

- [x] 审计 GNSS datum→camera→target→WGS-84 均值链，确认杆臂只应用一次；
- [x] 姿态、mount 与像素扰动同时生成 ray/camera-offset NED 端点；
- [x] camera slant、DEM、ground range 和 bearing 共用同一端点集合；
- [x] 增加短距离大杆臂下旧 ray-only derivative 失真的回归；
- [ ] 用真实安装杆臂和姿态协方差完成室外已知真值覆盖率验证。

## Field-evidence bindings strict schema（v77）

- [x] 显式声明八项输入 hash/bytes 与时间策略绑定；
- [x] 显式声明 target-event、recording、camera-mount 与 calibration 绑定及 null 边界；
- [x] 禁止未知 binding，覆盖 pending/complete 正测与额外 claim 负测；
- [ ] 用真实现场八层输入生成 strict-schema-valid 报告并由第二机独立重算。

## Target-event 相机标定跨证据绑定（v76）

- [x] 将严格相机标定复制进 replay bundle，并绑定 ID、bytes、SHA-256；
- [x] 加载时重新哈希 contained 文件并执行完整内参/外参语义校验；
- [x] capture CLI 支持显式标定输入，field bundle 强制 replay 与 mount survey 标定一致；
- [x] 覆盖文件篡改、缺失 provenance 与同 ID 不同内容的 fail-closed 回归；
- [ ] 用真实最终安装标定生成现场 target-event、mount survey 与 accuracy 三者一致的证据链。

## 测量设备校准 provenance（v75）

- [x] 定义设备 ID、issuer、有效期、精度与嵌套证书哈希的严格记录；
- [x] 提供 `equipment-record` CLI 自动生成记录，避免人工计算证书哈希；
- [x] survey 独立检查有效期覆盖测量时间、身份/精度一致和证书未变化；
- [ ] 现场绑定真实量具证书并确认其校准有效期覆盖实际测量日期。

## Mount-survey 现场工作表与预检（v74）

- [x] 原子生成固定 v1 CSV 空白模板，默认拒绝覆盖与 symlink；
- [x] 提供创建正式证据前的只读 raw CSV 预检与严格 JSON Schema；
- [x] 覆盖空白模板失败、填充后通过、CLI round-trip、样本数和身份边界；
- [ ] 使用真实测量设备填写模板并执行现场预检、独立复核和正式 evidence 创建。

## 原始安装测量单一真源（v73）

- [x] 固定 mount-survey CSV v1 列顺序和数值解析边界；
- [x] 生成器与 verifier 只从 hash-bound CSV 取得统计样本；
- [x] API 显式 samples 与 CSV 不一致时 fail-closed，CLI 移除分离输入；
- [ ] 现场按模板采集真实最终安装状态数据并由第二人复核。

## 物理相机安装外参与 lever-arm 证据门（v72）

- [x] 定义至少 5 次 FRD 重复测量、设备精度、独立 surveyor/reviewer 和物理声明；
- [x] hash-bind 设备校准证书、原始测量记录和 camera calibration，并由独立 verifier 重算；
- [x] 将 `camera_mount` 加入 field evidence 第七层和 shared task parity；
- [x] 对文件篡改、身份重合、声明缺失和 uncertainty 低报执行 fail-closed 回归；
- [ ] 在最终安装状态的真实飞机上完成测量、复核并生成新报告；该现场项不得由测试 fixture 代替。

目标平台是一架通用折叠串联固定翼无人机。固定摄像头、Jetson、Pixhawk 和数传始终安装；灭火载荷是可选任务模块。软件从配置中的载荷清单自动推导能力：空清单只执行巡检告警，有载荷时才允许进入安全检查和人工授权流程。

## 干净 HEAD Combined provenance 收口（v71）

- [x] 在提交 `73429e505d33ca5c645be3d3d0b6cda667f33c5f`、`dirty=false` 的工作树上重跑 900 帧 Combined；
- [x] 原生目标点击、相关 ACK、TRACKING/QML TRK、832 份 range/geolocation、336 合格坐标和
  496 fail-closed withheld 全部在同一 run 中完成，0 operator transport error；
- [x] 独立 verifier 44/44、VTOL 7-item Mission、MC/FW/MC、land/disarm、容器与端口清理全部通过；
- [ ] 完成四模式人工视觉 0/4、真实 Jetson/Pixhawk/相机、室外 GNSS/RTK、HIL/REAL 与实飞门。

## 原生目标点击→测距→GPS 落图 Combined 闭环（v70）

- [x] 由 QML 候选框导出几何，在 exact QGC PID/HWND 上执行真实点击并验证 `SELECT_TRK` ACK；
- [x] 强制 ACK、TRACKING、QML TRK adoption 绑定同一 command/target；
- [x] active operator target 接入共享 ranging/geolocation，GPS/测距不合格时保持 0 假点；
- [x] 修复极小多源 contribution wire 权重量化溢出，并增加主来源+微小 VIO 回归；
- [x] 当前 parity 900 帧 Combined：831 range + 831 geolocation、112 合格坐标、0 transport error；
- [ ] 完成 REPLAY/SIM/HIL/REAL 四份人工视觉、真实 Jetson/Pixhawk/相机、室外 GNSS/RTK 与实飞门。

## 当前源码 combined 独立传感器硬门（v69）

- [x] combined source-side 强制 CameraInfo K/P/R/D 验证状态；
- [x] 独立 verifier 强制 CameraInfo status、sensor expected/received/complete 与逐帧 full odometry；
- [x] 三类字段篡改负测和 E7 combined fixture 完成；
- [x] 当前源码真实 900 帧 source 通过增强 verifier 44/44，全仓 `3308 passed / 1 skipped`；
- [ ] 完成四模式人工视觉 0/4、HIL/REAL、真实硬件、室外 GNSS/RTK 与实飞门。

## CameraInfo 语义与 900 帧联合复验（v68）

- [x] 将 Gazebo `CameraInfo` 的 K/P/R、PLUMB_BOB、D 与仓库 camera calibration 逐字段绑定；
- [x] 将 canonical calibration hash 纳入 backend sensor identity，并把验证状态传播到 runtime/frame-set；
- [x] 用真实 Gazebo 三帧诊断确认 32-bit intrinsic 量化边界，保持有界 `1e-4 px` 容差与严格 R/D；
- [x] 当前源码 `c0a4c32` 完成真实 900/900 sensor、RTSP、type-23/type-24、odometry 和 7-item Mission；
- [x] combined source 独立 verifier 41/41、全仓 `3305 passed / 1 skipped`、ruff、容器/端口清理通过；
- [ ] 完成 REPLAY/SIM/HIL/REAL 四份人工视觉（当前 0/4）、HIL/REAL、真实硬件、室外 GNSS/RTK 和实飞门。

## 900 帧联合 SIM 时间绑定收口（v67）

- [x] 将 Gazebo Harmonic `CameraInfo` 明确定义为会话级静态标定，保持正时间戳、因果性和逐帧几何
  一致性检查，不再套用动态遥测 expiry；
- [x] NavSat/Odometry 继续执行 1.5 秒动态时效门；独立 callback 短暂错序时丢弃不安全图像，持续
  不新鲜时由有界 helper timeout fail-closed；
- [x] 当前源码 `005da90` 完成真实 900/900 Gazebo image、RTSP、signed type-23、QGC post-adoption
  type-24；4 张错序图像被丢弃后恢复，0 帧绑定旧动态遥测；
- [x] combined source 的独立 verifier 41/41 通过，容器移除、9 个测试端口释放、14550 不变；
- [ ] 完成 CameraInfo K/P/畸变语义标定验证，以及 REPLAY/SIM/HIL/REAL 四份人工视觉、HIL/REAL、
  硬件和室外 GNSS/RTK 门。

## pre.8 QGC 四模式人工视觉 E7 收口（v66）

- [x] 将 exact PID/HWND 机器报告、PNG、QGC EXE 和当前 `ui_contract_hash` 绑定为可复验输入；
- [x] 12 项人工可见性 checklist、明确 reviewer/UTC/decision/attestation 和内容摘要完成；
- [x] 近地平线负控必须至少 5 帧有检测、全部 range invalid 且 0 个假坐标；
- [x] 严格 JSON Schema、独立 verifier、CLI、模板和篡改/缺项/拒绝回归完成；
- [x] E7 writer/evaluator/schema/CLI/wrapper 接入 source-bound 人工记录集合，严格要求
  REPLAY/SIM/HIL/REAL 四模式各一份且禁止复用 machine report/screenshot；
- [x] 将机器捕获升级为显式模式的 schema-v2 通用契约，绑定 EXE/parity/run/PID/client rect/遮挡/PNG；
  历史 PX4 acceptance 仅作为 HIL legacy adapter，不再被 REPLAY/SIM/REAL 复用；
- [ ] 为 REPLAY、SIM、HIL、REAL 分别完成独立机器捕获和人工逐项复核（当前 0/4）；v61 PNG 只可作为
  HIL 候选输入，不能重复用于其他模式。

## pre.8 三机型 PX4 动态响应与故障 campaign 收口（v63）

- [x] fixed-wing/multirotor/VTOL 使用同一 AI→MAVLink `MANUAL_CONTROL` 路径驱动 external PX4/Gazebo；
- [x] 只从 `GLOBAL_POSITION_INT` 验证实际位移，禁止 Gazebo truth consumption 和 pose injection；
- [x] source-bound 三机型矩阵、独立 verifier、JSON Schema、E7 writer/evaluator/CLI/wrapper 接线完成；
- [x] 当前 parity、external join 和 report-only E7 均按 v63 源码重新生成；
- [x] 每机型两次正常响应与三种显式故障均完成外部运行，source-bound campaign、独立 verifier、
  JSON Schema 和 E7 子门通过；
- [ ] 完成 operator visual、部署域模型、现场链路、HIL/REAL 与真实三机型硬件反馈。

## v61 户外定位/SIM/QGC 动态机器捕获收口

- [x] 将三轴射线不确定性失败映射为 Python/QGC 共用的 `RANGE_STATUS` bit 31，保持内部诊断细粒度并
  让操作端稳定显示 fail-closed 状态。
- [x] 为三种机型场景加入仓库自带 GeoJSON 地图和严格 `.dem.json` 网格，校验场景 ID、WGS-84、
  Gazebo world origin、网格边界、文件 containment 与 SHA-256。
- [x] 将 map/DEM 纳入 simulator aggregate asset identity、外部 runtime 环境和 VTOL acceptance 的
  真实文件 availability 判断。
- [x] 在共享 `MultiModalRangingEngine` 中加入局部 DEM 高程/坡度采样、射线-地形交点和坡度不确定性
  传播；REPLAY/SIM/HIL/REAL 必须复用同一实现。
- [x] 将同一合格 raw GPS 样本的绝对 MSL 高度贯通 Pixhawk、live、raw replay 和 GNSS sidecar；配置
  DEM 后，时间偏斜/越界/无交点禁止平地回退，并避免同源 AGL 二次融合。
- [x] 增加 `--ranging-dem` CLI、严格 WGS-84/MSL/原点/100 km 域校验和三机型 v52 确定性 hash binding。
- [x] 建立非平坦 `terrain_ranging_validation_001` SDF/DEM 对，并从 SDF transform 独立恢复解析平面
  truth；41 个表面样本与 7 条射线（6 条跨单元、1 条垂直基准）通过严格误差门。该结果只关闭
  deterministic asset-geometry 子门。
- [x] 在外部 PX4/Gazebo 中实际加载非平坦 world，采集 image/NavSat/Odometry，并由 qualified NavSat
  MSL − scenario DEM MSL 产生 AGL；v54 关闭 sensor-runtime/高度基准子门，但该记录只在 home 采样。
- [x] 在 x500 disposable PX4/Gazebo 中让 truth-free synthetic target 经过共享 perception → AI →
  `MANUAL_CONTROL` 路径，观察 PX4 `GLOBAL_POSITION_INT` 最大 `0.818282 m` 位移；v55 不使用
  truth/pose injection，只关闭单机型 reported-position response 机器子门。
- [x] 将 reported-position 响应扩展到 fixed-wing/multirotor/VTOL，各执行两次正常运行，并为每机型
  注入 target-refresh dropout、heartbeat dropout、mode loss；v63 共 6 positive + 9 fault，负向运行
  保持 runtime 成功后由对应安全门 fail-closed，campaign 已接入 E7。
- [x] 对 v55 current-code 执行全仓库回归：2,907 tests 中 `2906 passed, 1 skipped, 0 failed, 0 errors`；
  唯一跳过为 Windows symlink 权限项，JUnit SHA-256 为
  `A2C2595C49D0B663468D09274D22976CC1B4D9128425BB23458445B1C5397F51`。
- [x] 使用 5 帧真实 Gazebo BGR24 camera 像素经过 quarantined candidate、共享 semantic ground-contact
  anchor、DEM/GNSS ranging 与 WGS-84 target geolocation：5/5 落图，最大水平误差 `0.674040 m`、
  最大帧间漂移 `0.035197 m`；近地平线负向 5/5 不落假点。两份 evidence 均由当前输入/parity
  独立重跑复现。该结果只关闭单高度 software/SIM 算法子门。
- [x] 将 semantic image anchor 纳入 `core_algorithm` parity，重新生成五组共享契约并从六份不可变
  external source 重建三机型 join/revalidation；当前 E7 report-only 仍为 32 blockers。
- [x] 对 v57 current-code 执行全仓库回归：3,233 tests 中 `3232 passed, 1 skipped, 0 failed, 0 errors`；
  唯一跳过仍为 Windows symlink 权限项；JUnit 469,702 bytes / SHA-256
  `2FD79F5349A56DBDB72EB6AD1A77653707310D141F99CF996A36FFA3A23A9B6F`。
- [x] 将单场景定位 evidence 升级为 evaluator-source-bound schema v2，并新增机器可读工况矩阵门：
  source 必须逐份完整复算且 release/evaluator identity 一致；正向覆盖要求 3 场景/15 帧、3 高度档、
  3 距离档、2 坡度档，同时保留至少 1 场景/5 帧的零坐标负样本。
- [x] 用当前真实正/负 source 运行 v58 矩阵：负向 `1/1` 场景、`5/5` 帧满足；正向仅 `1/3`
  场景、`5/15` 帧，高度/距离/坡度仅 `1/3`、`1/3`、`1/2`，因此按设计 fail-closed。
- [x] 对 v58 current-code 执行全仓回归：3,241 tests 中 `3240 passed, 1 skipped, 0 failed, 0 errors`；
  JUnit 470,851 bytes / SHA-256
  `08FEBF2327722653503715CF1180ACA2FABEE7578F5937D734430C6CCC9FE7F0`，Ruff check/compileall 通过。
- [x] 补采 mid/high slope 两组实际 Gazebo camera/NavSat/Odometry，各 5 帧；与原平地工况组成
  `6.20/12.17/20.15 m` AGL、`8/15/25 m` 距离、`0/4.00/7.97 deg` 坡度三档。每份定位 evidence
  及独立 verifier 通过，近地平线负控保持 0 假点，v59 matrix `gate_passed=true`。
- [x] 有界处理 Gazebo 高出生点首个 Odometry 速度派生毛刺：只在首个有效样本/首帧前最多丢弃 8 个，
  预算耗尽、有效样本之后或已收帧后同类异常均 fail-closed；补齐成功和耗尽反向测试。
- [x] 对 v59 current-code 执行全仓回归：3,251 tests 中 `3250 passed, 1 skipped, 0 failed, 0 errors`；
  JUnit 472,512 bytes / SHA-256
  `9F62B10AA176760E9F4CB8C4431CAF62EA9E28748D22C42C749D7C942DD100E1`，Ruff、compileall、
  diff check 与 parity byte-compare 通过。
- [x] 将通过 v59 gate 的 high-slope evidence 最后一帧重绑定到实时 HIL target/frame，以签名
  type 10 range + type 22 WGS-84/1σ 送入现有原生 QGC；实际 acceptance 认证 40 包、两类定位状态
  各 1 包，PX4 heartbeat 6，所有写入/物理输出 0，14550 不变且资源全清理。
- [x] 对 v60 current-code 执行全仓回归：3,255 tests 中 `3254 passed, 1 skipped, 0 failed, 0 errors`；
  JUnit 473,177 bytes / SHA-256
  `0BD69A52C24CC8EC1438988CBB54A2223DFC656FB10D31F6C80D18B0DA725BE5`，Ruff、compileall、diff check
  及 parity SHA 对比通过。
- [x] 将 evidence 五组 parity 与 canonical 文件强绑定；在授权后 bounded refresh type 10/22，并让
  capture 脚本附着 acceptance 的同一个 QGC PID/HWND，而不是启动无状态副本。
- [x] 以至少 `1600x900` 原生窗口取得 exact-client PNG；机器图显示完整坐标/1σ、类别、距离、方位、
  更新时间、复制/快照/记录和只读声明。QGC 受控退出为 0，PX4/router/14550/端口/物理输出门全部通过。
- [x] 对 v61 current-code 执行全仓回归：3,257 tests 中 `3256 passed, 1 skipped, 0 failed, 0 errors`；
  JUnit 473,487 bytes / SHA-256
  `E5D307C7F4AC57A4198358FBF84CFD034B27C20F415F2D1B7BF8F49850CB0837`。
- [x] 已在 v130 独立机械提交完成 Ruff baseline；首次重扫为 433 个候选、282 个 canonical 差异，并已
  重新生成 parity、重算定位/三机型 external 派生证据、重跑 E7 与全量回归；v131 又从 Git 对象验证
  Windows 干净检出可直接通过 format gate。
- [ ] 基于 v61 已通过的 exact PID/HWND 机器图，使用 v64 review contract 由操作者逐项复核 evidence
  card/地图点/1σ 圆/复制/快照并生成 accepted record；独立 verifier 通过后再接入 E7。机器截图本身
  不冒充人工签字。
- [x] fixed-wing 与 VTOL 对等动态 response evidence 已与 multirotor 一并纳入 source-bound matrix 和
  repeatability/fault campaign，并已重跑当前 E7 wrapper；该 SIM 子门不改变硬件或实飞 blocker。
- [ ] 使用真实 GNSS、已测相机外参和多距离/多坡度 survey truth 完成室外定位验证，再进入 HIL/REAL。

## Phase 1: software-only mission loop

### 1. Unified aircraft configuration

- [x] One mission configuration supports zero or multiple payload slots.
- [x] Zero payload means patrol-only; it is not treated as exhausted inventory.
- [x] RGB-only patrol may issue a clearly bounded fire alert after multi-frame confirmation; payload-capable deployment keeps independent corroboration and safety gates.
- [x] Disposable missions still require exactly one approved non-hazardous payload.
- [x] Deployment remains simulation-only and human authorization cannot be disabled.
- [x] Live payload mode can complete an explicitly enabled operator-triggered `FakePayloadPort` HIL cycle.
- [x] Connect the authenticated localhost inert-controller exchange above `FakePayloadPort` through
  a mission adapter; bind the consumed authorization and scene identity, map rejection/timeout to
  terminal fail-closed states, and retain mandatory independent confirmation after `EXECUTED`.
- [x] Add a separately keyed independent bay/departure-sensor HIL message with distinct sensor
  identity, release/slot/time/sequence binding, mutation replay protection and fail-closed evidence
  handling; controller module identity cannot be registered as the independent source.
- [x] Carry independent confirmation over a separate localhost UDP evidence channel that emits no
  commands, ignores invalid datagrams, and cannot fabricate success on timeout.
- [x] Add an explicit live-camera two-channel inert-HIL coordinator. It requires the existing
  operator-triggered simulation flag, three different environment-only keys/key IDs, distinct
  controller/sensor identities, bounded waits and localhost-only transports; the default in-memory
  fake cycle remains unchanged.
- [x] Add fixed-wing minimum/maximum ground-speed limits and a still-air, point-mass HIL
  release-window planner that returns advisory `WAIT/READY` metadata only.
- [x] Bind the configured HIL release window into the safety decision and authorization scene
  digest; moving outside the window invalidates readiness.
- [x] Add `release-window-check` for geometry-only bench checks with explicit no-control output.
- [x] Add a one-command software acceptance report covering patrol alert-only, authorized
  fixed-wing `FakePayloadPort` HIL and overlapping-person fail-closed behavior.
- [x] Add a separate Mode-2 end-to-end acceptance that uses one authenticated, signed loopback
  session for target selection, unique independently confirmed fire resolution, continuous-slide
  confirmation, status return, separate authorization and exactly one simulated release. Verify
  person/ordinary-vehicle exclusion, target-switch and timeout revocation, and person-entry veto
  with no flight-control or physical payload interface.
- [x] Add Mode-3 end-to-end signed HIL for both vehicle and person selections. Require a continuous
  target-bound slide before centering/approach advice, and prove occlusion, loss, `AVOID`, `INVALID`,
  target switch and old-confirmation replay enter or preserve a latched advisory abort with no
  actuator interface.

Acceptance evidence:

```powershell
python -m multidetect validate-config configs/missions/fire_patrol.demo.json
python -m multidetect replay configs/missions/fire_patrol.demo.json examples/fire_mission_replay.jsonl
python -m multidetect replay configs/missions/fire_suppression_fixed_wing.demo.json examples/fire_fixed_wing_hil_replay.jsonl --simulate-authorized-cycle
```

The replay must emit one deduplicated `fire_alert`, remain in `searching`, create no authorization challenge and submit no fake release request.
The fixed-wing HIL replay must expose a `READY` advisory window, create a redacted human
authorization challenge, submit exactly one fake request only after approval, require both simulated
feedback sources and finish in `return_requested` with physical control disabled.

### 2. Live patrol console

- [x] Show frame-by-frame detections, confirmed tracks, dwell time and mission capability.
- [x] Show an immediate fire-confirmed banner.
- [x] Add a persistent event list, target queue, telemetry strip and map-ready position panel.
- [x] Add alert acknowledgement and visible delivery failure state.
- [x] Keep camera buffering and duplicate-frame history bounded so a long patrol cannot accumulate
  an unbounded image queue or frame-ID set; frame timestamps remain strictly increasing.

### 3. Data-link software boundary

- [x] Produce a versionable JSON fire-alert envelope in real time.
- [x] Provide recording and JSON Lines publishers for tests and local integration.
- [x] Audit successful and failed live alert delivery attempts.
- [x] Add an optional SQLite outbox that persists before send and retries pending alerts after restart.
- [x] Bound delivered outbox retention transactionally while never pruning pending or failed alerts.
- [x] Stream live audit events to disk with fsync while bounding the in-memory event window.
- [x] Add correlated receiver ACK validation and bounded exponential retry/backoff with a loopback HIL transport.
- [x] Add an HMAC-SHA256 authenticated UDP sender/receiver with signed correlation, timestamp checks and persistent receiver deduplication.
- [x] Run the camera/ONNX/tracker task and ground receiver as separate local processes: 105 predictions, one authenticated alert, one correlated ACK, delivered aircraft outbox row and matching ground dedup row.
- [x] Prove restart recovery with the real UDP adapter: ground offline leaves the original alert pending; the next run retransmits the same alert ID before new frames and clears it only after authenticated ACK.
- [x] Add a bounded authenticated alert-link bench that emits new alert IDs, retry/missing-ACK accounting and ACK P50/P95 metrics; loopback output is explicitly software baseline and cannot satisfy a hardware gate.
- [x] Hash-bind a passing REAL authenticated alert-link bench into the E7 manifest; re-open and re-verify its source report on every evaluation while keeping prerelease manifests backward-compatible.
- [x] Bind hardware-mode ACK evidence to the OS-selected local UDP source address and an explicit
  expected source CIDR before transmitting; wrong-interface reports stop at zero transmissions.
- [x] Pin every authenticated UDP sender socket to the preflight-selected source address and require
  route/CIDR/transport-source agreement before the hardware ACK loop can transmit.
- [x] Bind every successful ACK receipt to the sender socket's runtime `getsockname()` IPv4 and
  require complete source observations in hardware bench schema v4.
- [ ] Implement and validate the authenticated ACK transport on the selected real data link.

### 4. Model and replay validation

- [x] Add manifest verification for artifact SHA-256, model version, class order, strict Nx6 fields, prohibited uses and production approval.
- [x] Add a `model-check` gate for SHA-256, active provider, strict `N x 6` output and synthetic latency.
- [x] Bind runtime box-coordinate interpretation to the model manifest contract.
- [x] Bind fire and safety-object artifacts to distinct manifest roles; runtime rejects a fire-candidate manifest supplied as person-safety evidence.
- [x] Verify every supplied model manifest before loading any ONNX graph, and keep person-detector health false unless safety-object role evidence is verified.
- [x] Stream normalized predictions and inference latency for every processed frame.
- [x] Add a frame-aligned IoU evaluator for per-class/overall precision, recall, false alarms, misses and P50/P95 latency.
- [x] Add an optional GNSS-to-camera body-NED lever-arm correction to the shared ranging/WGS-84
  projector; rotate it with roll/pitch/heading, propagate survey sigma into ground/bearing
  uncertainty, persist it in ChArUco calibration provenance, and keep zero-valued legacy files
  byte/behavior compatible.
- [x] Propagate the common camera-mount survey sigma independently through pitch/yaw/roll NED ray
  endpoints for range and bearing, and fail closed when any one-sigma mount-axis endpoint crosses
  the horizon; keep REPLAY/SIM/HIL/REAL on the same ranging implementation.
- [x] Run the full local-camera path through a real constant-output ONNX HIL artifact: 120 frames, one multi-frame-confirmed patrol alert, zero authorization/payload events and complete prediction/audit logs.
- [x] Add a hash-bound model review envelope that joins the candidate manifest, post-NMS `N x 6`
  model-check report, deployment-domain accuracy report and four independent governance decisions;
  it never mutates the manifest, and synthetic HIL remains ineligible for production approval.
- [ ] Add a reviewed post-NMS `N x 6` fire/smoke ONNX artifact and manifest.
- [ ] Build day, night, haze, reflection, smoke-only and no-fire replay sets.
- [ ] Measure deployment-domain per-class precision, recall, false alarms, missed detections and latency with the completed evaluator.
- [ ] Calibrate confidence and multi-frame confirmation thresholds from deployment-domain data.
  A reproducible per-class sweep and lighting/content strata report now exists for the local
  validation set; deployment-domain RTSP imagery is still required for this gate.

Phase 1 exit criteria:

- Patrol-only and payload-capable configurations both pass automated tests.
- Local camera and recorded video can run continuously without unbounded frame backlog.
- Confirmed fires are tracked and emitted once per configured cooldown window.
- Every alert and authorization change is auditable.
- No module can send flight-control or physical release commands.

## Phase 2: Jetson and RTSP integration

- [x] Add a hardened unprivileged systemd deployment template for patrol-only Jetson operation.
- [x] Add a credential-redacting static Jetson profile preflight that checks the patrol-only mission,
  model class/coordinate contract, RTSP syntax, read-only Pixhawk endpoint and authenticated alert
  settings without opening hardware or ONNX Runtime.
- [ ] Install and validate the package on the actual Orin Nano/JetPack image.
- [x] Implement deterministic TensorRT → CUDA → CPU provider ordering and bounded suffix fallback
  when default session construction fails; explicit provider lists remain strict and selection attempts
  are emitted in model-check/Jetson bench evidence.
- [ ] Validate the provider order and failover on the actual Orin Nano/JetPack image.
- [x] Add bounded capture/inference P50/P95 metrics, average FPS and reconnect counters.
- [x] Add configurable RTSP/local-camera reconnect attempts without a stale-frame queue.
- [x] Keep credential-bearing RTSP URIs out of service process arguments and redact them from application camera errors.
- [x] Add multi-frame `camera-check` with FPS, capture P50/P95 and reconnect reporting; local 640×480/120-frame smoke test passed with zero reconnects.
- [x] Raise the Jetson soak gate to 60 minutes/54,000 frames with >=15 FPS, <=66.7 ms inference
  P95 and a capture-queue high-watermark of at most one; the prior 30-minute artifact is retained
  only as a baseline and no longer satisfies the verifier.
- [x] Sample process RSS throughout the soak after a 60-second warmup; reject missing time-series
  evidence or more than 256 MB robust start-to-end growth, and report the hourly regression slope.
- Validate RTSP reconnect, end-to-end video-overlay latency, dropped frames, thermal load and power budget on the target Jetson.
- [x] Show camera/model health and reconnect count; audit and stop on acquisition or inference failure instead of treating it as an empty scene.
- Current sensor profile is RGB-only. Thermal synchronization is out of scope unless the hardware
  is explicitly changed in a future revision.

Exit criteria: representative RTSP video runs for the required mission duration with measured latency, temperature, power and reconnect behavior.

## Phase 3: Pixhawk V6X integration

- [x] Begin with a read-only MAVLink provider that never transmits heartbeat, commands, parameters, missions or stream requests.
- [x] Decode geographic position, relative altitude, attitude, speed, heading, battery, satellite count, armed state, flight mode and mission sequence.
- [x] Display link/position freshness, aircraft coordinates and a relative aircraft trail in the live console.
- [x] Add a standalone `pixhawk-check` sampler that proves the diagnostic path transmits zero messages.
- [x] Passively parse a complete `AUTOPILOT_VERSION` only from the accepted heartbeat
  system/component, redact raw `uid2`/`uid` behind a domain-separated SHA-256, expose version age and
  rejection counters, and add a zero-transmit `--require-autopilot-version` diagnostic gate.
- [x] Promote the sanitized `AUTOPILOT_VERSION` identity hash into newly provisioned signed HIL/REAL
  RuntimeProfiles and all six Mission-feedback E7 leaves while keeping legacy profile documents
  readable for diagnostics; provision now requires a same-source version frame observed during its
  own receive-only, zero-transmit probe window.
- [x] Separate legacy RuntimeProfile diagnostics from flight authority: every HIL/REAL launcher and
  REAL output request now requires a profile-bound, live-matching firmware identity, and the
  low-level flight provider independently rejects firmwareless profiles.
- [x] Separate launcher-probe authorization from actual-provider liveness: every new flight provider
  starts output-locked, must independently re-attest a fresh same-source heartbeat and firmware
  identity before any stream request or control output, and revokes/clears that attestation on
  timeout, stable identity change or close. CLI, Mode-3 approach status and fixed-wing audit events
  consume this dynamic state instead of a startup snapshot.
- [x] Make the native QGC Mode-3 banner consume the same dynamic authority: “aim active” requires
  current flight-control enablement, while locked/stale/abort/cancel states explicitly state that no
  attitude or mode command is being sent. Keep the shared Fly View and QGC-native controls.
- [ ] Capture `AUTOPILOT_VERSION` from the real V6X and use that exact identity in six independent
  HIL/REAL × fixed-wing/multirotor/VTOL profile/report leaves.
- [x] Add a separate, single-use `pixhawk-param-backup` path that requires explicit acknowledgement,
  sends exactly one `PARAM_REQUEST_LIST`, preserves PX4 bytewise integer values and raw wire bytes,
  rejects incomplete lists, and has no parameter-write, command, mission or actuator send path. Its
  real pymavlink localhost UDP HIL passed; it has not been run against the real V6X.
- [x] Add strict offline `pixhawk-param-verify` and `pixhawk-param-diff` commands. Verification
  rejects schema/invariant/value/hash tampering; diff denies every unlisted change by default and can
  require named expected changes. Both transmit zero messages and explicitly state that the embedded
  list hash is self-consistency evidence, not cryptographic authentication. The explicitly
  authorized real-V6X read completed 1102/1102 parameters with one request, zero writes and zero
  flight/mission/actuator messages; the real parameters were not changed.
- [x] Add an offline `pixhawk-link-audit` that separates GR01/TELEM1 at 115200 from the
  Jetson/V6X board-internal Ethernet primary path and the optional UART1/TELEM2 921600 fallback.
  The real snapshot passes both required primary configurations; TELEM2 is reported as disabled,
  optional and not physically verified. The audit opens no transport and sends zero messages.
- [x] Add a read-only mission-lifecycle gate: wait for fresh link/position, armed state, approved auto mode and configured mission sequence before entering search.
- [x] Verify the board-internal Ethernet path on the real V6X: `192.168.0.3:14550` produced only
  MAVLink 2 from system/component `1:1`; the project receiver matched PX4 fixed-wing identity and
  transmitted zero messages. The current `UNINIT` state and absent global position remain correctly
  blocked as flight-readiness issues rather than being misreported as a link failure.
- [ ] Compare every displayed field and freshness transition with QGroundControl on the real V6X link.
- [x] Add a read-only, HMAC-authenticated file HIL contract for allowed-area, geofence-health and release-zone-clear evidence, bound to mission ID, fresh Pixhawk position, timestamp and monotonic sequence; every validation failure restores unknown predicates.
- [ ] Replace the file HIL bridge with independently validated real geofence, allowed-area and deployment-zone evidence; generic telemetry must not imply permission.
- [x] Keep route execution, stabilization, failsafe and return-to-launch inside Pixhawk/PX4; the Jetson lifecycle observer sends no commands.
- [x] Exercise the real pymavlink serialization and UDP receive path with a telemetry-only
  fixed-wing HIL emitter: all displayed fields matched, 29/30 samples had fresh link/position,
  stale transition was verified, the receiver transmitted zero messages, and zone predicates
  remained unknown.
- [x] Run one combined Live software HIL with real pymavlink UDP input, authenticated position-bound
  zone evidence, observed armed/AUTO/mission-sequence lifecycle, signed G20 UDP selection and
  authorization, automatic authenticated inert-controller feedback and a separate independent
  confirmation UDP channel. Jetson transmitted zero Pixhawk messages; camera/model and all hardware
  remained simulated.
- [x] Run the official PX4 fixed-wing SIH/SITL process through the strict passive receiver and the
  Live lifecycle path. The original run exercised a local camera; the repeatable script now fixes
  the source to clock-paced `synthetic://patrol`, opens no local/network camera, and retains the same
  unarmed `LOITER` fail-closed assertions with zero authorization, payload and transmitted MAVLink
  events. Stopping SITL makes the fresh-link gate fail closed on isolated UDP 14650.
- [x] Add an explicit, Docker-confined positive patrol option. It arms only the newly created SITL
  process, permits `LOITER` for gate-plumbing HIL, observes `standby -> navigating -> searching`,
  emits one deduplicated patrol alert, creates no authorization/payload action and disarms before
  cleanup; Multi-Detect still transmits zero MAVLink messages. It is not an AUTO mission test.
- [x] Exercise the complete observed lifecycle through an armed PX4 AUTO mission in an owned,
  disposable software-only SITL container. Mission upload, parameter overrides, arm and mode
  changes are confined to that container; Multi-Detect remains receive-only and observes Mission
  sequence progress, movement and one patrol alert with zero authorization or payload action. The
  current no-camera run processes 900 deterministic frames at about 29.69 FPS with 900 target-pool,
  short-term-tracking, patrol-advisory and monocular-avoidance updates; avoidance is valid after its
  single warmup frame, capture backpressure is zero, and no camera device or URL is opened. The same
  armed `MISSION` run now retains ten identities through
  `DETECTED -> LOCKED -> TRACKING -> OCCLUDED -> REACQUIRING -> RECOVERED`, reaches LOST only after
  the conservative branch, recovers the same ID with strong ReID in 0.15 s, retains the background
  lock and emits only a human-confirmed return-observe advisory. The advice remains `DEGRADED`
  because generic SITL telemetry does not prove geofence health; application MAVLink writes remain 0.
- [x] Validate PX4 datalink-loss Hold behavior in the same strict container boundary. With a
  loopback-only GCS heartbeat and SITL-only `COM_DL_LOSS_T=5`, `NAV_DLL_ACT=1`, the fixed-wing
  transition was `MISSION -> LOITER`; heartbeat recovery cleared `gcs_connection_lost` but retained
  LOITER until an explicit operator mode change. Protected UDP 14550 was unchanged, Multi-Detect
  transmitted zero MAVLink messages and no real V6X or payload controller was contacted.
- [ ] Repeat the AUTO mission and datalink-loss acceptances with a PX4 SITL build exactly matched to
  the real V6X firmware. The current pinned `v1.18.0-beta1` SIH evidence is interface/failsafe
  evidence only and does not validate real airframe behavior.

Exit criteria: loss or staleness of required telemetry is visible and prevents deployment readiness without affecting Pixhawk failsafes.

## Phase 4: payload hardware-in-the-loop

- [x] Define and validate a versioned read-only inventory evidence contract with HMAC, key ID, timestamp, monotonic sequence, rollback rejection and same-sequence content consistency for the file-based HIL bridge; device key provisioning remains pending.
- [x] Verify module identity, exact slot IDs/types, lock state, controller/interlock health and independent presence sensors.
- [x] Fail closed in live mode when the payload controller or inventory evidence is unavailable.
- [x] Revoke pending or approved authorization when inventory becomes unknown or mismatched.
- Test only with inert, non-hazardous loads in HIL and a restrained ground rig.
- [x] Inject software/HIL disconnects, duplicate/mismatched inventory, stale authorization, failed slot/interlock, wrong feedback, timeout and uncertain release; physical jam testing remains pending.
- [x] Define an HMAC-authenticated, bounded inert-load HIL request/result protocol bound to
  mission, module, slot/type, release ID, consumed authorization identity, target revision, scene
  digest, ruleset, expiry and monotonic sequence. It rejects tamper, replay mutation, stale data,
  concurrent slots and success reports with unhealthy interlocks.
- [x] Validate the protocol across a real localhost UDP socket with bounded identical-request
  retry, first-response loss, controller-side idempotent result caching, authenticated rejection,
  and no implicit execution unless the inert-HIL flag is explicit.
- [ ] Select and connect an independently reviewed real controller transport; the localhost HIL
  transport is not mission-connected and has no serial, CAN, GPIO, PWM, MAVLink actuator or
  physical output.
- Never allow perception output to call a release actuator directly.

Exit criteria: every uncertain or inconsistent condition stays locked, no automatic retry occurs, and two independent feedback sources are required for confirmation.

## Phase 5: controlled field validation

- Complete hazard analysis, operational approval and test-site procedures.
- Validate launch, patrol, tracking, approach, abort, egress and return separately.
- Use approved inert loads before any actual fire-suppression payload.
- Add the real non-hazardous payload only after independent safety review.

## Phase 6: G20 / GR01 operator link

- [x] Confirm the documented camera stream, GR01 interfaces, serial rates and radio bandwidth.
- [x] Define strict target-selection and tracking-status domain messages without a live transport.
- [x] Keep target selection separate from payload deployment authorization.
- [ ] Bench-test simultaneous RTSP clients through an onboard Ethernet switch.
- [ ] Verify bidirectional IP packets between Jetson and the custom G20 application over GR01.
- [x] Make the GR01 selection/ACK bench bind the OS-selected sender IPv4 to an expected aircraft
  CIDR, fail with zero transmissions on a wrong route, and independently verify its strict report.
  This closes the software evidence contract only; the preceding physical GR01 check remains open.
- [x] Define a strict, hash-bound selected-link topology contract. Hardware ACK evidence now binds
  the GR01-direct endpoint/source/identity snapshot; selecting V6X fallback requires hashed GR01
  failure, PX4 SITL routing and actuator-disconnected V6X bench evidence before any UDP send.
- [x] Implement and deterministically mutation-fuzz the compact signed `TUNNEL` payload codec, bounded ACK retry, TTL, correlation and replay protection.
- [x] Wrap the codec in signed, addressed MAVLink2 `TUNNEL` byte frames; verify software
  round-trip, source/target identity, tamper rejection, replay rejection and unrelated-message rejection.
- [x] Provide a no-control UDP diagnostic server/client and verify a real localhost socket round-trip.
- [x] Add a separate compact read-only mission-status stream for phase, safety, authorization
  display state, payload count and fixed-wing release-window advisories; verify HMAC,
  MAVLink2 and localhost UDP round-trips without adding a control path.
- [x] Add a separate 86-byte read-only safety-status stream with a versioned 19-rule registry,
  present/pass/deny/unknown masks, target/ruleset binding, HMAC, signed MAVLink2, UDP round-trip,
  change-driven delivery and a 1 Hz unchanged heartbeat.
- [x] Add and validate a platform-neutral G20 viewport transform for aspect-fit letterbox,
  center-crop, black-bar rejection and 0/90/180/270-degree source/display box round trips; wire
  boxes remain normalized to the original source frame.
- [x] Add a separate authenticated G20 authorization challenge/decision/ACK contract with nonce
  confinement, full published-snapshot binding, bounded retry, replay protection, one decision per
  challenge and a final current-safety recheck; approval only advances mission state and never
  requests physical release.
- [x] Add Mode-2 payload-target challenge/continuous-slide/ACK/status messages bound to both the
  selected object and the independently confirmed fire aimpoint. Complete Python/UDP/QGC golden
  vectors, a native QGC slider, Windows Release compilation, and Windows plus Jetson headless HIL;
  real QGC-to-Jetson touch interaction remains a separate bench gate.
- [x] Close the equivalent Mode-3 signed selection/slide/status loop for arbitrary tracked classes
  and rerun it headlessly on Windows and Jetson Python 3.10. Keep all centering, approach and bounded
  climb outputs advisory-only; real QGC touch and PX4 SITL flight behavior remain separate gates.
- [x] Add a native QGC custom-build source skeleton with raw-video-local target overlay, explicit
  touch-selection mode, patrol/payload display modes, tracking/safety/authorization panels and a
  deterministic software-only UI simulator. Compile-time and QML invariants keep flight-control
  writes and physical release disabled.
- [x] Compile and run the custom desktop QGC source against the pinned Qt/MSVC toolchain. A concurrent
  isolated HIL with real PX4 fixed-wing SITL and a metadata-only Jetson component completed target
  selection, tracking/safety state, challenge-bound approval and ACK with QGC exit code 0. The strict
  router forwarded only read/status traffic and kept flight, parameter, mission, actuator and payload
  writes disabled.
- [ ] Visually validate the 1280x720-to-1920x1200 viewport vector and all UI transitions on a real
  1920x1200 G20-class render; the deterministic transform tests and desktop HIL do not replace this.
- [ ] Implement and bench-test the authorization drawer in the custom Android QGC build with an
  explicit operator identity, evidence review, approve/deny confirmation and link-loss behavior. The
  ARM64 APK now contains the drawer and passes host tests/lint; real G20 interaction remains pending.
- [ ] Validate actual bidirectional routing over GR01 direct IP or, if required, the V6X fallback path.
- [ ] Install the built Android ARM64 QGC custom Fly View overlay on G20 and validate the 1920x1200
  coordinate transforms, H.265/RTSP path and touch behavior. Package/ABI/signature/static validation
  is complete; no ADB device was present for the real-device gate.
- [x] Validate targeted operator `TUNNEL` isolation in PX4 fixed-wing SITL: QGC `255/190` metadata for
  Jetson `1/191` remained local to the operator loop and was never forwarded into PX4.
- [ ] Repeat the targeted routing check on an actuator-disconnected real V6X only if GR01 direct IP is
  unavailable; keep all control and payload writes disabled.

The detailed topology, UI proposal and acceptance sequence are in
[`g20-gr01-integration.md`](g20-gr01-integration.md).

The deployment planner may recommend an approved fire-area deployment region and a validated release window. It must not target people or vehicles, and human authorization remains mandatory. The current planner is explicitly HIL-only: it omits wind, terrain, calibrated payload aerodynamics and aircraft response, so it cannot establish a physical release solution.
