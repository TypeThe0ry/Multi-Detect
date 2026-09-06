# Multi-Detect 当前进度

最后更新：2026-09-06（v111 工作树）

## v111 本轮已收口

- 修复安装主命令与仓库 wrapper 的能力漂移：`multidetect evaluate-replay-set` 现在同样公开
  `--prediction-run-receipt`，并原样转交 v110 evaluator verifier；
- synthetic HIL ONNX 端到端回归先生成真实 runtime predictions/receipt，再分别调用 wrapper 与主 CLI，
  两者都必须返回相同 receipt canonical hash；以后删参或漏转发会直接失败；
- 未修改 detector、测距/定位、QGC、共享 parity 或飞控路径，因此不重建 portable EXE；
- 全量回归 3,510 tests：`3509 passed, 1 skipped, 0 failed, 0 errors`，耗时 192.917 秒；JUnit
  511,970 bytes、SHA-256 `C0E68ED6656C4AA59D7FEEB00D3BCF9C8328787E325FA68C527F26A1638EB637`；
  全仓 Ruff、compileall 与 diff check 通过。

## v110 本轮已收口

- accuracy evaluator 新增可选 `prediction-run.receipt.json` 输入；计算指标前调用 prediction 独立 verifier，
  复验 annotation/raw/truth/model/predictions/new manifest 全链，而不是只信 accuracy 调用参数；
- model artifact SHA、model ID 和 model version 从已验证 receipt 派生；调用者若同时提供冲突值会 fail-closed，
  防止另一模型或另一轮 predictions 被拼进同一份精度报告；
- accuracy report 新增 receipt 字节 SHA、canonical hash、model manifest SHA、runtime inference 与 synthetic HIL
  事实绑定；closed schema 已同步，未使用 receipt 的旧评估路径保持兼容；
- readiness fresh recomputation 会取出并再次验证同一 receipt，再比较完整 `prediction_run_binding`；因此静态
  accuracy JSON 即使自改模型字段，也不能绕过新鲜重算；
- CLI、报告写入和 synthetic HIL ONNX 端到端测试已覆盖 receipt→evaluator→readiness 链及冲突拒绝；
- 全量回归 3,510 tests：`3509 passed, 1 skipped, 0 failed, 0 errors`，耗时 191.635 秒；JUnit
  511,970 bytes、SHA-256 `AE8EA9C38A226E570DCED72A5C9722F71236B731DDC34166B8C039FFE347F145`；
  全仓 Ruff、compileall 与 diff check 通过。

## v109 本轮已收口

- 新增固定模型 replay prediction runner：先复验 v108 annotation bundle 与 model manifest/artifact，再复用正式
  `ReplayFrameSource`、`OnnxNx6Detector`/`OnnxRawYoloDetector`、`JsonlPredictionWriter`，没有离线算法分叉；
- 六个 canonical strata 严格按 recording 帧序生成 predictions，模型类别 canonical 化后必须与 reviewed replay
  完全一致，未知输出类别、帧缺失/重复/错序均 fail-closed；
- 输出采用不可覆盖 sibling temporary；bundle 内复制并绑定固定模型，保留原 annotation manifest，另生成包含
  predictions 的新 manifest 和 canonical receipt；receipt 锁定模型身份/hash、输入/NMS/threshold、provider、
  runtime 版本、逐层文件 hash/帧数及 P50/P95/maximum/total latency；
- 独立 verifier 重开 raw/intake/workspace/truth/原 annotation manifest/model/manifest/predictions/新 manifest 全链；
  test double、真实 runtime、production approval、synthetic HIL 和 accuracy/release claims 明确分离；
- installed CLI、clean-checkout wrapper、closed receipt schema 与 4 项回归已落库；实际 synthetic HIL ONNX 集成
  smoke 已经通过 OpenCV、ONNX Runtime、letterbox、Nx6 adapter 和 `fire→flame` 规范化；
- 全量回归 3,510 tests：`3509 passed, 1 skipped, 0 failed, 0 errors`，耗时 194.679 秒；JUnit
  511,970 bytes、SHA-256 `9BC817EB6611FAD799302D9EAC66658E8ABBCB45CF48D6A6373339B0963C75F9`；
  全仓 Ruff 与 compileall 通过。

## v108 本轮已收口

- 从 v107 hash-bound intake 生成逐帧 annotation workspace；每帧默认 `unannotated/objects=null`，未处理帧
  不能自动成为负例；
- positive 强制至少一个 class-bound、有限且归一化的正面积 bbox，negative 强制空对象；未知字段、重复 key、
  NaN/Infinity、类别越界和非法 bbox 均 fail-closed；
- assignment hash 绑定 intake/dataset/classes/frame/image，但 finalizer 还会从 intake 重新推导并逐项比较，
  因此修改 frame ID、stratum、image path/hash/尺寸后重算自报 hash 也不能通过；
- finalize 要求 annotator/reviewer 分离和 UTC review timestamp，在 sibling temporary 原子导出六份 truth、
  reviewed workspace、review receipt 与 deployment replay manifest；verify 重开 raw/intake/workspace/truth/manifest
  并做语义和逐字节对账；
- create/inspect/finalize/verify CLI、installed entry point、两份 closed schema 与 4 项回归已落库；所有输出继续
  `predictions_complete=false`、`accuracy_validated=false`、`release_ready=false`，不替代模型回放和精度评审；
- 全量回归 3,506 tests：`3505 passed, 1 skipped, 0 failed, 0 errors`，耗时 177.668 秒；JUnit
  511,437 bytes、SHA-256 `445FFF48AC1B2A99051EF6490C96D6FCECD96130870A12414992D50ACD5ABE99`；
  全仓 Ruff 与 compileall 通过。

## v107 本轮已收口

- 新增 deployment replay-set intake：六个 strata 必须显式绑定六个不同 raw recording，拒绝目录复用与
  recording hash 复用，不靠目录时间或通配符猜来源；
- staging 只复制 raw manifest、events 和被引用图像，不携带源目录中的旧 ground truth/predictions；逐文件
  SHA-256/字节数、recording identity、frame count 和完整 artifact inventory 全部绑定到 canonical receipt；
- 新目录采用 sibling temporary + 完整回验 + rename，拒绝覆盖、symlink、路径逃逸、重复 JSON key、NaN、
  receipt/claim/artifact 篡改；失败会清理临时目录；
- receipt 明确固定 annotation/prediction 为 pending，四项 review/accuracy/release claims 全为 false；后续人工
  标注与模型输出仍须走现有 manifest/evaluator/readiness，不产生部署批准；
- 新增源码 API、installed CLI、clean-checkout wrapper、closed JSON Schema 和 5 项回归。全量回归 3,502 tests：
  `3501 passed, 1 skipped, 0 failed, 0 errors`，耗时 176.837 秒；JUnit 510,892 bytes、SHA-256
  `452AC95871E4746943EDD900A60DA9B545E3403193DC75FAE96988D194EC57A8`；全仓 Ruff 与 compileall 通过。

## v106 本轮已收口

- E7 replay-set evaluator 现在从已绑定 manifest/accuracy report 调用 canonical readiness checker，输出
  hash-bound、fresh-recomputed 的逐 entry/stratum 统计、检查、原因和下一步动作；
- readiness 是嵌套诊断投影：不以自身缺少 accuracy report 否决已经通过的独立 threshold calibration，
  同时在读取/重算异常时仍 fail-closed；专项回归覆盖 pending manifest 和 calibration-only 兼容路径；
- 当前 SIM fixture 复算为 6 entries、6 strata，文件和 prediction streams 完整，但 6/6 为
  `synthetic_demo`、6/6 `annotations_reviewed=false`，正向 strata 没有正真值/正预测；readiness 记录
  37 条原因与 7 个下一步，顶层 E7 保持 32 个真实 blockers；
- 五组 release parity 未改变，QGC C++/QML 未改变，因此不重绑 SIM 摘要、不重建 portable EXE；
  全量回归 3,497 tests：`3496 passed, 1 skipped, 0 failed, 0 errors`，耗时 323.705 秒；JUnit
  510,223 bytes、SHA-256 `FE6A5E094765BF4A5E46EA29A2013D1773E7D2E1631018548E8362F91BC0C4E7`；
  全仓 Ruff 与 compileall 通过。

## v105 本轮已收口

- 修复 `run_current_release_gate_pre8.ps1` 固定默认链的历史漂移：将缺少 CameraInfo K/P/R/D 与完整
  odometry 语义的旧 combined source 换为当前 900 帧 acceptance，并绑定当前 release-mode envelope；
- 从六份不可变 external runtime/AI 原始记录重算 join/revalidation，从三机型正向源重算 reported-position
  matrix，并从 6 次正向与 9 次故障注入源重算 campaign；全部绑定当前 release parity 且独立 verifier 通过；
- 按当前 Gazebo model/root 字节重新生成 fixed-wing、multirotor、VTOL deterministic 3-D control records，
  清除 v56 model SHA 漂移；不宣称外部 3-D、人工视觉、硬件或实飞验收；
- wrapper 仅指定新输出目录/时间戳即可生成 E7 manifest/report，结果为 32 个真实 blocker，且不再因旧
  parity/model hash 异常退出；默认仍固定路径、拒绝 mtime/通配选择，现场报告过期时继续 fail-closed；
- 本轮不改 release parity，也不改 C++/QML，因此不重建 portable EXE；全量回归 3,497 tests：
  `3496 passed, 1 skipped, 0 failed, 0 errors`，耗时 145.857 秒；JUnit 510,223 bytes、SHA-256
  `69A47ECAA68596D342876978CB22C2C60E35F71C4178F0CAC199476D1EC39F06`；全仓 Ruff 与 compileall 通过。

## v104 本轮已收口

- release manifest writer 从 canonical field-link verifier 生成 `real_field_link.address_state_bound`，E7
  无需下钻源 JSON 即可看见 REAL 链路是否携带地址 apply provenance；
- evaluator 每次重开 field-link 报告及其嵌套地址回执，将实际 binding presence 与 manifest 投影对账；
  顶层布尔篡改、嵌套地址源漂移均 fail-closed；
- prerelease 保留旧 manifest 可读性；最终 `0.3.0` 必须显式携带该投影，但允许值为 false，以兼容由
  DHCP、预置静态地址或其他受控机制配置的现场网络，避免把特定 helper 错当唯一部署方式；
- release-gate schema/writer/evaluator 纳入 PX4 Mission parity，hash 更新为 `7CD5CD2B...B32402F`；
  本轮没有生成硬件、HIL、REAL 或实飞接受声明；全量回归 3,497 tests：
  `3496 passed, 1 skipped, 0 failed, 0 errors`，耗时 172.340 秒；JUnit 510,223 bytes、SHA-256
  `6EB9E2180B44649CB0F7C23ACF5C1ABE9C490B27AC349694463F829FBCB6F90D`；全仓 Ruff 与
  compileall 通过。

## v103 本轮已收口

- field-link 预检 CLI 与 Windows wrapper 新增可选地址回执绑定：只接受成功 `apply` 证据，并要求显式
  interface alias、目标地址/prefix、父级 expected source CIDR 与最多 300 秒的新鲜度；
- 预检报告保存回执路径、字节数、SHA-256、schema、接口和地址身份；canonical verifier 每次重新打开
  源回执，按父预检观测时间复验，保证归档报告仍可确定性验证；
- 源文件变化、binding 篡改、软链接、自引用、what-if 冒充 apply、策略时限放宽、地址越界、父 CIDR
  缺失及 interface diagnostic alias 不一致全部 fail-closed；地址回执不替代远端 endpoint probes，也不
  单独升级 `hardware_link_ready`；
- 当前机器只执行未绑定的只读 wrapper smoke，仍为 Ethernet `192.168.1.13/24`、飞机网段端点不可达、
  `hardware_link_ready=false`；没有 apply 地址、触发 UAC 或生成 HIL/REAL/flight 接受声明；
- 安全 parity 更新为 `D5B15BF4...FDBC87E`，其余四组 hash 不变；新增 7 项地址/绑定回归；全量回归
  3,496 tests：`3495 passed, 1 skipped, 0 failed, 0 errors`，耗时 112.331 秒；JUnit 510,081 bytes、
  SHA-256 `D6665848313861046A7C2FD496D68178C28F87A66758D81C80FBF155094C007F`；全仓 Ruff 与
  compileall 通过。

## v102 本轮已收口

- 新增 closed schema `field-link-address-state.schema.json`，统一 plan/verify/what-if/apply 四类 PowerShell
  输出；`prepare_field_link.ps1` 现在写入固定 schema identity；
- 新增 `field_link_address_state.verify_field_link_address_state`、仓库 CLI wrapper、安装后 console entrypoint
  与根包 lazy export；复验过程只读，不接触网卡或远端设备；
- verifier 严格拒绝未知/重复字段、重复 JSON key、NaN/Infinity、非 canonical IPv4、重复地址、非法路由、
  错误 plan change/elevation、安全标志、rollback 动作/命令、目标接口/IP/prefix 和 stale/future 时间；
- apply 只有持久化地址状态与 postcondition 一致且成功时才过 gate；plan/verify/what-if 可结构通过，但
  `--require-applied` 必须失败。真实生成的三类非变更文档均已由 CLI 独立复验；本轮未修改主机网络；
- schema、producer、verifier、CLI 与实现全部纳入安全 parity，`safety_policy_hash` 更新为
  `10F115B1...DE7E8A`，其余四组 hash 不变；新增 9 组回归覆盖 schema、语义重算、篡改、时效与 CLI；
  全量回归 3,489 tests：`3488 passed, 1 skipped, 0 failed, 0 errors`，耗时 174.813 秒；JUnit
  509,138 bytes、SHA-256 `295860BFF1C36FB49C005AB2B3B2459D7F89BB26DFAC221617497E2E927A5BCE`，
  全仓 Ruff 与 compileall 通过。

## v101 本轮已收口

- 地址 helper 用 `$appliedChange` 记录 `ShouldProcess` 后实际执行的 add/remove；`-WhatIf` 明确输出
  `mode=what-if`、`changed=false`，且无需管理员权限，不再把 dry-run 误记成主机变更；
- 正常 apply/remove 后重新枚举精确目标地址。add postcondition 要求恰好一条匹配地址且
  `SkipAsSource=true`；remove 要求零条。审计 JSON 记录是否检查/是否通过，失败时命令非零退出；
- 本机真实非提权 `-WhatIf` 已验证：计划添加 Ethernet `192.168.144.250/24`，没有修改网络，审计
  记录 792 bytes、SHA-256 `08ACD59D...58E94`；
- 因 field-link helper 已纳入统一安全身份，`safety_policy_hash` 自动更新为
  `46324CC0...3694AF`；UI/core/task/PX4 Mission 四组 hash 不变，仍未产生硬件/HIL/REAL/实飞接受；
  全量回归 3,480 tests：`3479 passed, 1 skipped, 0 failed, 0 errors`，耗时 115.741 秒；JUnit
  507,924 bytes、SHA-256 `26BCF986E3CBFAD9481BF7005227E7861E76419A55E7A47461AE802691BE9307`，
  全仓 Ruff 与 compileall 通过。

## v100 本轮已收口

- `prepare_field_link.ps1` 新增非提权 `-PlanOnly`：在修改网卡前输出拟执行动作、是否需要提权、目标
  IPv4/prefix、secondary-only/`SkipAsSource` 约束、明确不变更 gateway/DNS/explicit route 以及精确
  rollback 命令；`-VerifyOnly` 同样可生成结构化只读状态；
- `-Output` 使用 UTF-8、write-new、flush-to-disk 语义保存审计 JSON，拒绝覆盖已有文件；输出路径会被
  精确传递给提权子进程。实际 apply/remove 仍要求管理员权限，本轮没有触发 UAC 或改变主机网络；
- 当前机器计划证据确认 Ethernet 仍为 `192.168.1.13/24`，拟添加的是
  `192.168.144.250/24` secondary address，`would_change=true`；只读 verify 与 3-sample field preflight
  继续显示飞机网段无可用 source route，Jetson/GR01/RTSP 均未就绪，`hardware_link_ready=false`；
- 将 field-link 接口诊断、地址准备、PowerShell/Python runner、canonical verifier 和聚合实现全部纳入
  `safety_policy` parity。安全 hash 更新为 `792296E8...C7B74`；其余四组 hash 不变。本轮没有 QGC
  C++/QML 变化，因此不重建 EXE，也不提升 HIL/REAL/hardware/flight 声明；全量回归 3,480 tests：
  `3479 passed, 1 skipped, 0 failed, 0 errors`，耗时 157.868 秒；JUnit 507,924 bytes、SHA-256
  `E04959F235BA971BDF7A66F654ADAD99DA9E0DD65C32947EE250FEE33CDF7FA9`，全仓 Ruff 与 compileall 通过。

## v99 本轮已收口

- `write_field_covariance_journal_index` 先生成稳定快照、write-new 输出，再从磁盘执行完整 index verifier；
  成功返回值来自 verifier，不再信任未落盘内存对象；
- 复验后使用同一 `indexed_at` 重建第二份当前快照并与首份逐字比较，捕获 build→write 窗口内新增
  session/probe、campaign 前移或证据内容漂移；
- 输出仍拒绝覆盖；只有本轮确认新建的 index 才会在复验失败时删除，journal、receipt、session、probe 和
  预存输出均不修改；测试覆盖写后 blocked probe 篡改和写后新增 probe 两条回滚路径；
- `px4_mission_path_hash=4BDEFEC0...BCC1A`，其余四组 parity 未变化。全量回归 3,480 tests：
  `3479 passed, 1 skipped, 0 failed, 0 errors`，耗时 101.403 秒；JUnit 507,924 bytes、SHA-256
  `EC912331EA105664692CAC0241E14D8989618A8D0DB302455654125376ADE1E6`；全仓 Ruff 与 compileall 通过。
  本轮无 QGC C++/QML 改动，未重建 EXE；真实 HIL/REAL/hardware/flight 门仍未通过。

## v98 本轮已收口

- 将 `blocked_probe_count` 从裸计数升级为 closed-schema `blocked_probes[]`：逐项绑定文件路径/SHA、online
  evidence digest、观测时间、HIL/REAL 模式、机型与失败原因；count 必须与数组长度一致；
- 每个未被成功 session 引用的 probe 必须通过 archived online preflight 独立复验且
  `gate_passed=false`；伪 JSON、篡改证据和未引用的 passing probe 均 fail-closed；
- index verifier 重开 blocked probe 并重算完整记录，删除、替换或漂移后旧 index 不再通过；成功 session
  引用的 probe 也被强制限制在同一 journal 目录；
- index 构建前后均要求 receipt/workspace 专属 active capture lease 不存在，并双读 campaign status、session
  文件集、probe 文件集及文件 SHA；采集中或构建中发生 report/artifact 漂移时拒绝混合快照；
- `px4_mission_path_hash=F24E5BAE...521A0`，其余四组 parity 未变化。全量回归 3,478 tests：
  `3477 passed, 1 skipped, 0 failed, 0 errors`，耗时 101.246 秒；JUnit 507,642 bytes、SHA-256
  `E617EB9F2B7227635604A37F0F807B4388F9EA2952E4A4752BDAD00FC8C7A464`；全仓 Ruff 与 compileall 通过。
  本轮无 QGC C++/QML 改动，未重建 EXE；真实现场/HIL/REAL/flight 证据仍为空。

## v97 本轮已收口

- 关闭“成功 session 已 write-new，但进程在 lease 正常 release 前崩溃”的审计断链：死亡 owner 的 active
  lease 经精确 recovery 后，成功 session 仍可由 journal index 入册；
- 每份 session 必须恰有一个 lease 审计源：正常路径为 `released-leases/<lease-id>`，异常恢复路径为
  `recovered-leases/<lease-id>`；两者同时存在或均不存在都 fail-closed；
- recovered 路径会独立重开并验证 `lease.json` 与 `recovery.json`，重算文件 SHA 和 canonical digest，核对
  lease ID、lease digest、完整 receipt 及 session 内嵌 lease；缺失、替换、篡改或重新签 digest 后的错误
  receipt/lease/hash 均拒绝；
- index record 的 closed schema 记录 archive kind、lease path/hash 和可空 recovery path/hash，并用条件约束
  强制 released 记录无 recovery、recovered 记录必须具备 recovery；`lease-status` 增加 recovered 数量；
- `px4_mission_path_hash=72CBC380...6FB4`，其余四组 parity 未变化。全量回归 3,474 tests：
  `3473 passed, 1 skipped, 0 failed, 0 errors`，耗时 100.000 秒；JUnit 507,101 bytes、SHA-256
  `2B0494192B80F67E859E18153FC5DE2D490737DCB192F73B6F2D60DB10803466`；全仓 Ruff 与 compileall 通过。
  本轮没有 QGC C++/QML 变化，因此没有无意义重建 EXE；真实现场断电/HIL/REAL/flight 门仍未通过。

## v96 本轮已收口

- 修复 v95 active lease 的两个断电窗口：acquire 先在非阻断 `.lease-candidates/<lease-id>` 中 write-new、
  fsync 完整文档，再原子 rename 整个目录到 active path；release 先原子移出 active path，再保留到
  `released-leases/<lease-id>`，不存在“空 active 目录永久卡死”状态；
- candidate 残留只计入 `nonblocking_candidate_count`，不会夺取 capture authority；managed candidate、
  released、recovered 目录全部拒绝 symlink；
- released lease 不再销毁。journal index 对每份 session 重开其 released `lease.json`，校验文件 SHA、
  receipt/campaign/leaf/digest，并要求与 session 内嵌 lease 完全一致；缺失或替换均 fail-closed；
- recovery 在写完 `recovery.json`、尚未 archive 时中断后可以安全重试，但必须使用相同精确 lease ID 与
  相同 reason；不同请求、已有目标 archive 或活/未知 owner 都拒绝；
- 当前机器完成新鲜只读 field-link 探测：Ethernet 为 `192.168.1.13/24`，飞机网段
  `192.168.144.0/24` 无候选接口/route，3/3 样本未就绪；报告结构与绑定独立复验通过，但
  `hardware_link_ready=false`，没有提升任何硬件声明；
- `px4_mission_path_hash=FD77EAC9...20BBDC`。全量回归 3,468 tests：
  `3467 passed, 1 skipped, 0 failed, 0 errors`，耗时 99.785 秒；JUnit 506,030 bytes、SHA-256
  `85CBAA7D6890737943A6A5A93CAD06E51C920941EE1B62552A2EC51CECEB7D15`；全仓 Ruff 通过。

## v95 本轮已收口

- 新增 `field_covariance_journal` 的原子目录 lease；`run-next` 在 online probe/capture/record 全事务外持锁，
  同一 receipt/workspace 的第二进程在采集启动前 fail-closed；成功、blocked probe 和正常异常路径均仅在
  lease 文档仍逐字一致时释放，锁被篡改则保留；
- `lease-status` 使用同机只读进程状态判断。Windows 走 `OpenProcess + GetExitCodeProcess`，不发送信号；
  远端 hostname、权限/状态未知或仍存活 PID 都禁止恢复；
- `recover-lease` 要求精确 32 位 lease ID 和书面原因，且 owner 必须被证明死亡；恢复会把原 lease 与
  closed-schema、digest-bound recovery 一起原子归档到 `recovered-leases/<lease-id>`，不静默删除证据；
- 成功 session 嵌入完整 lease，并在事后复验时交叉核对 receipt/workspace/campaign/leaf/digest；新增
  `journal-status/verify-index`，严格对账 session `1..N` 连续前缀与 campaign 已验证 reports，拒绝断号、
  重复、乱序、跨 receipt、路径逃逸和 campaign 回退，同时统计未进入成功 session 的 blocked probes；
- journal/session 两份 schema 与实现纳入 PX4 Mission parity，`px4_mission_path_hash=71A7B6D0...E20CEA`。
  真实硬件未连接，未生成现场 lease/session/index 或任何 HIL/REAL/hardware/flight acceptance。
- 全量回归 3,464 tests：`3463 passed, 1 skipped, 0 failed, 0 errors`，耗时 97.762 秒；JUnit
  505,454 bytes、SHA-256 `106EDE42C9C57A04EF6D30C864B2D876E2DF3923B82C07CEED40FE7E0AEF4FDC`；
  全仓 Ruff 通过。

## v94 本轮已收口

- 新增 `field_covariance_session run-next/verify-record`：每次从 v93 receipt 重验本机 workspace，锁定唯一
  `next_leaf`，只执行一叶 Pixhawk covariance 现场事务；
- `capture-next --probe-report-out` 先 write-new 保存 online observation，再从磁盘按默认 300 秒时效、当前
  leaf、source/parity、mode binding、identity 和 query budget 独立复验，通过后才启动 capture subprocess；
- blocked probe 保留诊断文件但 `capture_started=false`，不产生成功 session；成功 capture 会重开正式
  report，并将 receipt/workspace/campaign/probe/report/进度绑定进 closed-schema、digest-bound record；
- archived probe verifier 只放松“仍为 next leaf”条件，保留其余完整重算；旧 record 在后续叶推进后仍可
  验证，但归档 probe 不能再次授权 capture；
- 修复 v92/v93 生命周期冲突：移动介质/receive 输入仍精确限制 20 文件；收到的本机 workspace 只允许
  campaign 声明且正式验证通过的报告路径，未声明文件或失败报告仍 fail-closed；
- status output 不能覆盖 campaign/source/report/probe/binding。session schema/module 纳入 PX4 Mission parity，
  `px4_mission_path_hash=F8E8F4E0...4ACCE1`。真实设备未连接，未生成现场 session 或 acceptance。
- 全量回归 3,456 tests：`3455 passed, 1 skipped, 0 failed, 0 errors`，耗时 100.186 秒；JUnit
  504,362 bytes、SHA-256 `870BA2CB7B98CE62782AD5CE6FB24B4DD6B3E2E320D25351BBF49C1854732072`；
  全仓 Ruff 通过。

## v93 本轮已收口

- `field_covariance_workspace receive` 先重验移动介质 bundle，只复制 manifest 声明的精确 20 文件到目标
  同盘临时目录，再重验 workspace digest、当前 parity、相对路径 campaign 和六叶 source preflight，最后
  原子发布本机副本；
- destination/receipt 均为 write-new，拒绝覆盖、目标位于 source 内、receipt 位于 source/destination 内和
  symlink；回执写失败会删除本轮刚发布的 destination，源介质不变；
- 新增 closed-schema、canonical-digest-bound 的外部接收回执及 `verify-receipt`。拔除源介质后仍会重开
  本机 destination、逐文件 hash、campaign、parity 与 preflight；源绝对路径只作为事务 provenance；
- 回执核对 6 leaves、18 sources、20 JSON、下一条本机 preflight argv，且 live identity、covariance、
  hardware、HIL、REAL、flight 全部固定为 false；现场仍必须重新 probe/capture；
- receipt schema/module 已纳入 PX4 Mission parity，`px4_mission_path_hash` 更新为
  `6B5AB2EF...F85F02`。当前未接入物理介质或真实 Pixhawk，没有生成实际接收回执或 acceptance。
- 全量回归 3,445 tests：`3444 passed, 1 skipped, 0 failed, 0 errors`，耗时 96.462 秒；JUnit
  502,750 bytes、SHA-256 `D3DCDE46F027DD67C86D437AE879FAA11F14A4480C308B0F2F74CCDC49F7A31D`；
  全仓 Ruff 通过。

## v92 本轮已收口

- 新增 `field_covariance_workspace export/verify`，导出 HIL/REAL × 三机型精确六叶的 18 份 Mission、
  Mission feedback、signed runtime profile 原始字节，并生成只使用相对路径的 portable campaign；
- 导出前与临时目录原子发布前均运行既有六叶 source preflight。搬迁回归把完整目录复制到第二个根、删除
  原导出目录，再次加载 campaign 并执行真实 preflight，证明验证不依赖原绝对文件路径；
- 闭合 schema/manifest 绑定 20 文件全集、逐文件 size/SHA、六叶顺序、endpoint/baud、源 campaign
  fingerprint、当前 release parity 和 canonical workspace digest；额外文件、内容漂移、路径逃逸、ADS、
  反斜杠、symlink 与 parity 漂移均 fail-closed；
- bundle 明确不携带 online probe、covariance report 或 acceptance。接收机必须用本机连接重新运行
  `preflight -> probe-next -> capture-next`；endpoint/飞控/firmware/签名身份不会被便携层静默改写；
- `px4_mission_path_hash` 更新为 `B30A0DAE...AF266B`。当前仍没有真实六叶输入和串口设备，因此没有
  生成物理 handoff bundle 或任何 HIL/REAL、hardware、flight 通过声明。
- 全量回归 3,439 tests：`3438 passed, 1 skipped, 0 failed, 0 errors`，耗时 94.448 秒；JUnit
  501,885 bytes、SHA-256 `8714CCA8795DD428D9BEFB4323005E639B77D23445B895E5B1B8649418E46065`；
  全仓 Ruff 通过。

## v91 本轮已收口

- `probe-next` 输出升级为 `multidetect.pixhawk-covariance-online-preflight/v2` 闭合 observation，加入 UTC
  观测时间、canonical evidence SHA-256 和固定为 false 的 release/hardware/HIL/REAL/flight claims；
- source binding 覆盖 campaign、Mission、Mission feedback、runtime profile、online schema、campaign runner、
  covariance mode-binding authority 与当前五组 release parity；
- 新增 `verify-probe`：重开当前 campaign，要求相同 leaf 仍是 `next_leaf`、六叶 source preflight 仍全绿，
  并重算 authoritative mode binding、采样时序、firmware response、查询/TX 预算、reasons 与 gate；
- 文件加载默认 300 秒过期；未来时间、digest 篡改、未知字段、source/parity 漂移、campaign 前移和 symlink
  全部 fail-closed。该在线记录仍绑定本机绝对路径且不跨机复用；v92 仅搬迁静态六叶 workspace，接收机
  必须重新生成在线记录；
- `px4_mission_path_hash` 更新为 `792FFFBE...100780`。当前没有真实 campaign 配置、串口/PnP 设备或
  `field-inputs`，所以没有运行真实 probe/capture，也没有生成 HIL/REAL、hardware 或 flight 通过声明。
- 全量回归 3,430 tests：`3429 passed, 1 skipped, 0 failed, 0 errors`，耗时 93.046 秒；JUnit
  500,693 bytes、SHA-256 `EF040571787F7AAD5759548B60EC8CE66EB21E3E878F830AE36D060436D0E241`；
  全仓 Ruff 通过。

## v90 本轮已收口

- 新增 `field_covariance_campaign probe-next`：先要求 v89 六叶 source preflight 全绿，再只打开当前
  `next_leaf` 的 Pixhawk transport；
- probe 复用 covariance capture authoritative `build_mode_binding`，逐项核对 configured/resolved
  endpoint、HEARTBEAT 六元身份、当前 firmware hash、transport/qualified link、operational
  qualification、read-only 与 hardware-control-disabled；
- ranging stream 请求保持关闭；若 firmware 未被动广播，只允许最多三次
  `MAV_CMD_REQUEST_MESSAGE(512)` / `AUTOPILOT_VERSION(148)` 查询，并强制 TX 总数等于 attempts；
- 正式 `capture-next` 会再次运行 online probe；online gate 失败时明确输出 `capture_started=false`、
  `capture_verified=false`，不创建目录、不调用采集 subprocess；成功/失败都关闭 provider；
- online report 的 release/hardware/HIL/REAL/flight claims 固定为 false，它是现场操作状态而非 E7 证据。
  `px4_mission_path_hash` 更新为 `C7C86A1F...982AB218`。真实硬件当前不可用，因此没有伪造通过结果。
- covariance campaign/binding/capture 专项 62 项通过；全量回归 3,419 tests：
  `3418 passed, 1 skipped, 0 failed, 0 errors`，耗时 95.171 秒；JUnit 499,148 bytes、SHA-256
  `32D6BDC009476B67C1117A137E981642570A9004F4137626C1E66F8F180CAD53`；全仓 Ruff 通过。

## v89 本轮已收口

- `field_covariance_campaign preflight` 一次重开六叶 Mission、feedback v2 与 runtime profile，不访问
  transport，输出固定顺序的 `ready` / `blocked` 清单和三份 source SHA-256；
- 每叶核对当前机型 adapter ID/hash、HIL/REAL mode、configured endpoint、baud、feedback gate、
  receive-only/zero-transmit、runtime signature/profile gate、observed identity、flight-control disabled 与
  output locked；
- `capture-next` 在启动采集 subprocess 前强制要求全部六叶 READY，dry-run 也携带完整预检结果；源文件
  检查后发生变化会在下一次重新打开时被拒绝；
- 24 个 campaign runner 专项测试通过，覆盖六叶正例、八类错配、post-check source tamper、CLI 失败码和
  blocked 状态下 subprocess 零调用；`px4_mission_path_hash` 更新为 `3D3A77F6...CA52C95`；
- 全量回归 3,415 tests：`3414 passed, 1 skipped, 0 failed, 0 errors`，JUnit 498,573 bytes、
  SHA-256 `C370E39F2C7B2C93E609AFA5931E50603BED63D3DFECB2F68C0B78A105790E3C`；全仓 Ruff 通过；
- 这只关闭现场启动前静态配置缺口。当前 Pixhawk 在线 HEARTBEAT/firmware identity 仍由正式 capture
  验证；真实六叶 HIL/REAL、硬件、室外 GNSS、人工 QGC 与实飞均未声明，最终版本继续阻断。

## v88 本轮已收口

- 新增 `field_covariance_campaign` CLI 与严格 Draft 2020-12 配置 Schema，固定 HIL/REAL × 三机型六叶；
- `status` 从当前 source 重验每个已有报告并显示唯一下一叶；`capture-next --dry-run` 可在打开串口前
  审阅完整命令，正式调用每次只采一个叶子；
- 报告沿用 write-new 不覆盖；既有失败报告会中止 campaign。采集进程即使退出 0，runner 仍检查报告
  schema、source/parity、Mission/profile/feedback/link/firmware，失败则返回 1；
- 六叶通过后 `verify` 直接调用 v87 E7 builder，并生成完整 E7 CLI 参数；状态快照可原子更新，但不被当作
  release evidence；
- 提供 `configs/pixhawk-covariance-field-campaign.example.json`，Schema 与 runner 纳入 PX4 Mission parity。
  当前缺真实现场 source 与硬件，因此未执行 HIL/REAL 采集，最终版本保持阻断。
- `px4_mission_path_hash` 更新为 `E3371122...89B83`；全量回归 3,403 tests：
  `3402 passed, 1 skipped, 0 failed, 0 errors`，JUnit 496,778 bytes、SHA-256
  `DA2B4104B657A64051DB879E75B1C5DCD5B3C8ABDA90C02D242915A069BBA25A`；全仓 Ruff 通过。

## v87 本轮已收口

- 新增 `pixhawk_covariance` E7 六叶矩阵，覆盖 HIL/REAL × fixed-wing/multirotor/VTOL；预发布可缺省，
  一旦提供即 authoritative，最终 release 必须完整提供；
- 每叶不信任摘要字段，而是调用 schema-v3 observation verifier 重建报告，并重新打开 Mission、feedback、
  runtime profile；compact E7 记录与当前 source exact compare；
- binding 固定 runtime/airframe、report/source hash、Mission adapter、endpoint/baud、HEARTBEAT、firmware、
  current transport/qualified link、read-only/hardware-control-disabled 与 `px4_mission_path_hash`；
- manifest builder/API/CLI、严格 JSON Schema、evaluator、final-release blocker 和重复/篡改/source-drift
  回归已接线；parity 变化后旧 v86 SIM 报告按设计被共享 leaf verifier 拒绝；
- 真实六叶 HIL/REAL covariance 尚未采集，所以这轮关闭的是 E7 软件消费缺口，不宣称硬件、室外 GNSS、
  HIL/REAL 或实飞通过，最终 `v0.3.0` 保持阻断。
- `px4_mission_path_hash` 更新为 `AADF6672...41715`；全量回归 3,391 tests：
  `3390 passed, 1 skipped, 0 failed, 0 errors`，JUnit 494,984 bytes、SHA-256
  `A7FA11B6D5236372B0BC051B092E03F47AD899F25C95751C58F7ADA00C002A88`；全仓 Ruff 通过。

## v86 本轮已收口

- 新增严格 Draft 2020-12 covariance observation schema v3：顶层、record、diagnostics、request、
  heartbeat、firmware、HIL/REAL binding、checks、source binding 与 claims 全部闭合；
- writer 在原子写入前执行 Schema 验证，verifier 读取后先做 Schema 验证，再做 canonical digest、
  当前源码/source hash 与 derived checks/claims 的完整重建；
- 报告只保留 16 项门禁相关 diagnostics；motion/vertical/mission 等内部状态不再冻结进证据契约；
  tuple reasons 统一为 JSON array，缺 covariance 的失败报告仍能生成并被 verifier 复核；
- Schema path 与 SHA-256 直接进入 source binding，Schema 文件加入 PX4 Mission parity；`pixhawk`
  optional dependency 显式包含 `jsonschema>=4.20`；
- 当前 digest-pinned PX4 SITL 实采 223 条 ODOMETRY、223 条有效 covariance、保存 113 条记录；
  schema v3 写前/读后验证及独立 verifier 全部通过，容器已清理；
- Schema 22,231 bytes、SHA-256 `76DD77A2...A3B95`；证据 80,848 bytes、文件 SHA-256
  `98C7C297...AB02C`、evidence SHA-256 `C0579775...29A80`；
- `px4_mission_path_hash` 更新为 `6B1C9B22...F43D8`。HIL/REAL 实机、硬件、室外 GNSS、人工
  QGC 与实飞继续保持未通过，最终版本仍阻断。
- 全量回归 3,386 tests：`3385 passed, 1 skipped, 0 failed, 0 errors`；JUnit 494,307 bytes、
  SHA-256 `52AD5A17D952F5BA23A71631D9F4DAE59B43FF2C83AB461570ADC94590049376`；全仓 Ruff
  通过，历史 format 债务保持 412 个文件。

## v85 本轮已收口

- covariance report 新增 `transport_link_current`、`qualified_link_current` 与
  `autopilot_version_current_session` 三项硬门，拒绝断链缓存、未通过 operational qualification
  或不属于本次采集时窗的 firmware 身份；
- HIL/REAL `mode_binding` 同步加入 provider transport、qualified link 与 qualification 三项检查，
  即使 Mission/profile/firmware hash 匹配，当前链路不合格也不能形成现场 covariance 证据；
- 六个负回归覆盖 SIM 报告与 HIL binding 的 stale/unqualified 状态；相关 Pixhawk/capture 专项
  101 项通过；
- 当前源码在 digest 固定 PX4 SITL 独立进程实采 220 条 ODOMETRY、220 条有效 covariance、保存
  111 条记录；新增三门均通过，发送严格为 `3×7+1=22`，verifier 通过且容器已清理；
- 证据文件 81,010 bytes，SHA-256 `B2A1462F...DC3C8`，evidence SHA-256
  `23A37D0F...A3BC9`；firmware identity 为 `397A4F3B...73081`；
- `px4_mission_path_hash` 更新为 `7D18DFA5...5F514`。HIL/REAL 实机、硬件、室外 GNSS、人工
  QGC 和实飞声明保持未通过；严格 covariance JSON Schema 作为下一独立迭代。
- 全量回归 3,379 tests：`3378 passed, 1 skipped, 0 failed, 0 errors`；JUnit 493,089 bytes、
  SHA-256 `63351A22D8074BAD318A2796A8429CAEE70A22815189E0E3008D9903DF0CC5F7`；全仓 Ruff
  通过，历史 format 债务保持 412 个文件，没有因本轮增加。

## v84 本轮已收口

- Pixhawk provider 新增默认关闭的有界 firmware identity 请求：绑定 HEARTBEAT 后通过
  `MAV_CMD_REQUEST_MESSAGE(512)` 请求 `AUTOPILOT_VERSION(148)`，每个机体身份最多 3 次、5 秒退避，
  合法响应到达后立即停止；
- heartbeat 五元身份变化会清除旧 firmware，并重置该新身份自己的请求预算；默认 receive-only 模式
  继续零发送，整个路径不触碰参数、模式、Mission、执行器或 stream rate；
- covariance observation 要求 identity request 实际发生、response observed、firmware hash 合法，且
  总发送严格等于 `stream attempts×7 + identity attempts`；三次仍无响应则 fail-closed；
- 当前源码/当前 parity 的外部 pinned PX4/Gazebo 实采 207 条 ODOMETRY、207 条有效 covariance、保存
  112 条记录；PX4 第一次请求即返回 firmware，发送计数为 `3×7+1=22`，verifier 通过并清理容器；
- firmware identity hash 为 `8f527860...fdc81`，raw UID 没有进入报告；`px4_mission_path_hash` 更新为
  `3f118e57...4069c`。HIL/REAL 实机、硬件、室外 GNSS、人工 QGC 和实飞声明保持未通过。
- 全量回归 3,373 tests：`3372 passed, 1 skipped, 0 failed, 0 errors`；JUnit 491,817 bytes、
  SHA-256 `0CBB1A09D4D21F774A174078CAD68AABA686C32BA9AC709F6520F59ABCE4A996`；全仓 Ruff 通过，
  历史 format 债务仍为 412 个文件，没有混入本轮功能提交。

## v83 本轮已收口

- covariance observation 升级为 schema v2；HIL/REAL 采集必须给出可执行 Mission、PX4 Mission
  feedback v2 和签名 runtime profile，不能再用孤立 `--runtime-mode` 标签冒充现场证据；
- 每次现场采集都会重新验证三份来源，并把 configured/resolved endpoint、baud、HEARTBEAT 五元身份、
  当前 `AUTOPILOT_VERSION` firmware hash、read-only、hardware-control disabled、output locked 与
  flight-control disabled 绑定到 canonical `mode_binding`；任一漂移 fail-closed；
- SIM 仍只允许隔离 local `udpin`，且禁止携带 HIL/REAL 身份材料；HIL/REAL 会拒绝 `udp`、`udpin`、
  `udpout` 等所有受保护 14550 端点；
- 外部 pinned Docker/PX4/Gazebo v2 实采 110 条 `ODOMETRY`、110 条有效 covariance、保存 83 条记录；
  最终 `1σ=0.504°/0.501°/6.981°`，独立 verify 通过，容器已清理；
- `px4_mission_path_hash` 更新为 `5a4c280f...af93`。这里只关闭现场采集的身份/防伪工具缺口；真实
  HIL/REAL、硬件、室外 GNSS、人工 QGC 和实飞仍未声明，E7 仍为 32 blockers。
- 全量回归 3,369 tests：`3368 passed, 1 skipped, 0 failed, 0 errors`；JUnit 491,247 bytes、
  SHA-256 `CE988ADBD106AD029427957C2722C413673D1EA9A986E76351EED278AC187F54`；全仓 Ruff 通过。
  format 检查仍是 412 个历史文件的独立基线迁移债务，本轮没有把机械格式化混入功能提交。

## v82 本轮已收口

- Pixhawk 只读 ingress 请求并解析 MAVLink `ODOMETRY` 331 的 21 项 pose covariance，提取姿态 3×3
  block，要求 finite、正方差和 PSD，并使用 `0.3°/0.3°/1.0°` 显式下限；
- covariance 必须来自已确认 HEARTBEAT 的同 system/component、`LOCAL_NED` parent、合法 child frame，
  且与当前姿态相差不超过 250 ms；无效新样本会清空旧 uncertainty，不复用假精度；
- `BODY_FRD` child frame 的 twist 不再映射为 north/east，避免机体系速度污染导航速度；
- 外部 pinned Docker/PX4/Gazebo 实采 64 条 `ODOMETRY`，64 条 covariance 通过，保存 47 条记录；最终
  raw angular variance 为 `6.9689e-05/6.8983e-05/1.6177e-02 rad²`，派生 `1σ` 为
  `0.478°/0.476°/7.287°`，source 为
  `mavlink_odometry_pose_covariance_local_ned_with_configured_floor`；
- 采集报告以 canonical SHA-256 绑定 provider、采集/验证脚本和 release parity，并从 raw records 与
  diagnostics 重建全部 checks/claims；SIM gate 通过、容器清理通过，HIL/REAL/硬件/室外/实飞仍未声明；
- `px4_mission_path_hash` 更新为 `16e8427a...e1930`。E7 仍为 32 blockers，最终版本仍阻断。

## v81 本轮已收口

- fixed-wing、multirotor、VTOL 各以 pinned Docker/PX4/Gazebo 外部进程采集 3 帧 `MDGZSIM3`；
- 三份 runtime/sensor gate、逐帧重构、record hash、frame-set manifest 与容器清理全部通过；
- 最终 roll/pitch/heading sigma 与来源进入 frame provenance 和 latest telemetry，验收器从原始 wire
  covariance 重算后逐项比较，来源或数值篡改会在 detector 前拒绝；
- 三类机型的原始姿态 covariance 实测均为 9 项全零，manifest 记录 nonzero count=0；系统应用
  `0.3°/0.3°/1.0°` 配置 floor 并明确标注来源，不声称收到 estimator covariance；
- `core_algorithm_hash` 更新为 `ed102767...53f`。E7 仍为 32 blockers，现场声明不变。

## v80 本轮已收口

- `MDGZSIM3` 固定 276-byte header 完整携带 Gazebo pose covariance 的姿态 3×3 block，并纳入 record hash；
- producer/backend 双端要求矩阵 9 项 finite、对称、正半定；缺失 36 项源矩阵或 malformed wire 拒绝；
- fixed-axis X/Y/Z covariance 经数值 Jacobian 转为 NED/FRD Euler `1σ`，配置 floor 在变换后应用；
- gimbal lock 使用 `180°/90°/180°` 保守预算；原始矩阵和实际 sigma/source 均进入逐帧证据；
- producer/backend 新增到 `core_algorithm_hash`。外部 Docker/PX4 v3 帧已在 v81 三机型重采；E7 仍为 32 blockers。

## v79 本轮已收口

- `VehicleTelemetry`/Replay/raw replay 可携带完整 roll/pitch/heading `1σ` 与来源，历史全缺失记录不漂移；
- Live 优先消费遥测 uncertainty，整组缺失时明确标记 `live_configured_fallback`，部分或无来源元组拒绝；
- VI、RGB-SLAM 与主 ranging 使用同一 pose resolver，避免三条链各自静默吃 dataclass 默认值；
- `RangeSolution` 和 operator status 记录实际 sigma/source；Gazebo wire-v2 当时丢弃 covariance 的缺口
  已明确保留给下一 wire 版本，并在 v80 由 wire-v3 关闭，不把消息名称误报为已接入实测协方差。

## v78 本轮已收口

- 确认杆臂均值只在 ranging NED offset 应用一次，WGS-84 投影没有重复叠加；
- 将每个姿态/mount/像素 `±1σ` 样本升级为 ray 与 camera-offset 成对几何端点；
- camera slant、DEM 交点、ground range 与 bearing sigma 共用端点传播，姿态扰动同时旋转杆臂；
- 新增短距离大杆臂负回归：旧 ray-only 模型会返回与零杆臂相同 sigma，新模型明确区分。

## v77 本轮已收口

- `field-evidence-bundle.schema.json` 的 `bindings` 改为严格、无未知字段的机器合同；
- 八项输入 hash/bytes、时间策略、target-event、recording、mount 与 calibration bindings 均有显式类型；
- pending 与 complete 报告均经 Draft 2020-12 验证，任意额外 claim 会被 schema 拒绝；
- verifier 仍重开所有来源并比较重算结果；结构通过不能代替来源真实性或 HIL/REAL 验收。

## v76 本轮已收口

- target-event replay bundle 新增 contained `camera-calibration.json`，manifest 绑定 calibration ID、
  bytes 与 SHA-256，加载时重新哈希并运行完整相机标定语义校验；
- capture CLI 支持 `--camera-calibration` 与 manifest 相对路径，现场采集无需手工计算哈希；
- field evidence 的 target-event 层现在必须携带标定，并与 camera-mount survey 的 calibration ID/SHA-256
  跨证据一致；缺失、篡改或同 ID 不同内参全部 fail-closed；
- target-event manifest schema 纳入 shared core parity。本轮仍不产生 HIL/REAL、硬件或发布声明。

## v75 本轮已收口

- 新增 `equipment-record` CLI 和严格 schema，自动绑定证书文件 hash、issuer、有效期、设备身份与精度；
- survey 重新打开 calibration record 和嵌套证书，机器检查测量时间窗口、设备 ID、角度/线性精度；
- 过期记录、错设备、证书文件事后变化全部 fail-closed，任意 hash 文件不再等同有效设备校准；
- schema 纳入 shared task parity；真实证书签发真实性仍需现场复核，不由自动 fixture 代替。

## v74 本轮已收口

- `template` 原子生成 5–1000 行 canonical raw CSV，默认拒绝覆盖和 symlink；
- `validate-raw` 在正式 survey 前检查 hash、列、数值、范围、最小样本数和 ID 唯一性，并输出严格
  `camera-mount-measurement-preflight.v1` 诊断；
- blank template fail-closed、填充后 preflight pass、源码树 CLI round-trip 和 schema 回归均覆盖；
- 预检 schema 纳入 task parity，且所有输出继续明确不构成 hardware/release/HIL/REAL 接受。

## v73 本轮已收口

- 原始 mount-survey CSV 变成唯一统计数据源；固定 v1 header，严格解析 sample ID 与六个有限数值；
- CLI 删除分离的 `--sample` 输入，独立 verifier 重新读取同一 hash-bound CSV 并重算；
- Python API 若显式 samples 与 CSV 任一字段不同，`samples_match_raw_measurements=false` 并 fail-closed；
- shared task parity 已重新生成。该收口不改变真实测量/HIL/REAL/硬件/最终发布状态。

## v72 本轮已收口

- 新增物理 camera mount / GNSS-to-camera lever-arm survey 生成器、严格 schema、源码树 CLI 和独立
  re-hash/recompute verifier；三类输入均 hash-bound，至少 5 次重复测量且 surveyor/reviewer 必须不同；
- field evidence bundle 新增第七个 `camera_mount` 强制层，并保存 survey、aircraft、camera、GNSS antenna
  与 calibration SHA-256 绑定；缺失、篡改、模拟值、声明缺失或 uncertainty 低报全部 fail-closed；
- survey 与七层 bundle 已纳入 shared `task_contract_hash`。自动 fixture 仅验证合同，不关闭真实测量、
  HIL/REAL、硬件或最终发布门。

GitHub 审计基线：`origin/main`，提交 `b5c3f10baf4cc9febaa3a10103752f202f3b0611`；本轮功能基线为
本地 `main` 提交 `5fb7303722a3ccd6d8b5df1a3de388e72490e4f2`（当前文档提交前领先远端 56 个提交）。

产品版本：`v0.3.0-pre.8`（最终 `v0.3.0` 尚未发布）

机器可读镜像：[current-progress.json](current-progress.json)

## 结论先行

v71 已在干净提交 `73429e505d33ca5c645be3d3d0b6cda667f33c5f` 上重跑完整
PX4/Gazebo/QGC Combined SIM 生命周期；source 明确记录同一 HEAD、`dirty=false` 和空 Git status
摘要，独立 verifier 44/44 通过。原生 QGC 的真实目标点击继续按
QML 候选框几何映射到 exact PID/HWND，发送一次 `SELECT_TRK`，验证 ACK 与后续 TRACKING 状态均与
同一 command/target 相关，再让该 operator target 驱动共享测距和目标定位。相机安装俯角同步修正为
35°，GPS/DEM/相机地面射线链恢复可观测区域；测距 contribution wire 权重改为总预算量化，避免极小
VIO 权重与主来源四舍五入后总和超过 1 而间歇性拒绝 packet。当前 parity 的权威 900 帧 run 中，
832 份 range 和 832 份 geolocation status 全部发出，336 帧合格落图、496 帧明确不可定位、0 transport
error，Combined、VTOL attached 与 7-item Mission 均通过。

当前仓库已经形成统一的感知、跟踪、测距、目标落图、QGC、数字孪生、任务规划、PX4 Mission、
operator link、安全和证据框架。v59 已让三组各 5 帧的实际 Gazebo BGR24 像素经过同一候选 ONNX、
共享语义地面锚点、DEM/GNSS 测距和 WGS-84 投影；三个场景覆盖约 `6.20/12.17/20.15 m` AGL、
`8/15/25 m` 目标距离和 `0/4.00/7.97 deg` 坡度，均由独立 verifier 重现。对应近地平线输入 5/5 帧
都拒绝生成坐标。v60 又把 high-slope evidence 的测距、坐标和 1σ 经签名 operator link 送入现有
原生 QGC。v61 在同一 HIL 进程内持续刷新 type 10/22，按 exact PID/HWND 采集无外部遮挡的目标地图
与证据卡片机器截图，并把证据与当前五组 release parity 严格绑定。v62 又让固定翼、多旋翼和 VTOL
通过同一 AI/MAVLink 路径驱动 disposable PX4/Gazebo，以 `GLOBAL_POSITION_INT` 证明实际位移，并将
可重放的三机型矩阵正式接入 E7。v63 又完成每机型两次正常响应与三种显式故障，共 15 份外部
PX4/Gazebo 源报告，并把 repeatability/fault campaign 接入 E7。v64 新增 source-bound QGC 人工视觉
review 合同、12 项 checklist、0 假点负控和独立 verifier。v65 已把它接入 E7，并强制要求
REPLAY/SIM/HIL/REAL 各自独立的机器来源与人工记录；当前仍是 0/4，人工签字和现场证据未完成。
v66 进一步把机器捕获抽成四模式共用的 schema-v2 契约，绑定实际 EXE、parity、run、exact PID/client
rect、遮挡和 PNG；历史 PX4 报告仅保留 HIL 兼容，REPLAY/SIM/REAL 不再伪装成 HIL 格式。v67 修正
Gazebo Harmonic 的传感器时间语义：CameraInfo 是会话级静态标定，NavSat/Odometry 仍受 1.5 秒动态门；
短暂 transport callback 错序只丢弃不安全图像。v68 又把实际 CameraInfo K/P/R/D、PLUMB_BOB 与仓库
标定逐字段绑定，并将语义状态带入 runtime/frame-set。v69 再由独立 verifier 强制该状态、sensor 完整
计数与逐帧 odometry。当前权威 900 帧联合 SIM 安全丢弃 6 张不新鲜动态图像，并以增强 verifier 44/44
通过；900 个有效帧全部取得 QGC post-adoption type-24 ACK。人工记录仍为 0/4。

最终版本仍然阻断。当前 E7 report-only 结果为 `gate_passed=false`、`release_allowed=false`，共
32 条 blocker，主要来自部署域数据/模型精度、现场链路、四模式人工 QGC/Mission、HIL/REAL、
三机型硬件控制和 prerelease 版本面。候选 YOLO 模型仍为 `quarantined`，不是生产批准模型。

## v71 干净 Combined 基线（v72 回归沿用）

- 在干净 `73429e5` 上完成新的权威 source
  `combined-sim-qgc-20260905T175019Z-64536`：259,353 bytes，SHA-256
  `5031EEFE75CF78D42AECCC04E990BB8F9975FBC6EBB12D4087F685C18652C8B6`；source 自证
  `git_head=73429e5`、`dirty=false`。
- 900/900 sensor、RTSP、signed type-23、post-adoption type-24 和 full odometry 通过；832 份
  range/geolocation 中 336 份可落图，496 份按质量门明确 withheld，0 operator transport error；
  最大 frame-to-acceptance `609 ms`，最大 ACK-to-metadata skew `235 ms`。
- exact PID/HWND 上第一次命中 `flame` 候选框；`SELECT_TRK` ACK、TRACKING 和 QML TRK 绑定同一目标。
  VTOL Mission 完成 MC/FW/MC、land/disarm，最大离家 `226.949 m`、最大相对高度 `26.078 m`；容器、
  9 个动态端口与受保护 14550 状态均正确清理。
- 独立 verifier 44/44：2,139 bytes，SHA-256
  `0C27451F88A0FD3655740523525D97BAE98D8D981EE0A3CC60DB8178B9A1136D`。
- v82 全仓回归为 3,358 tests：`3357 passed, 1 skipped, 0 failed, 0 errors`；JUnit 为 489,660 bytes、
  SHA-256 `e9306292...f81d9`、suite time 94.158 s；全仓 Ruff 通过。
- 该 run 只把 v70 的 current-parity dirty 机器证据提升为 clean-HEAD software/SIM provenance；人工
  视觉仍为 0/4，真实 Jetson/Pixhawk/相机、室外 GNSS/RTK、HIL/REAL 和实飞继续阻断最终版本。

## v70 本轮已收口

- 原生 QGC Fly View 候选目标支持 source-bound 点击，`SELECT_TRK` ACK、TRACKING 状态和 QML
  `TRK` adoption 必须属于同一 command/target；取消仍保持单击状态机语义。
- active operator target 进入共享 multimodal ranging；GPS/测距合格时发布 WGS-84/1σ，任何质量门
  不合格时发布明确不可定位状态，不落假点，也不产生飞控或载荷指令。
- Gazebo VTOL 相机与标定统一为 35° 俯角；range contribution 使用 254 单位总预算量化，保留每个
  非零来源且保证解码权重总和不超过 1。主来源约 0.99875、VIO 约 0.00125 的回归已覆盖。
- 当前 parity 真实 source `combined-sim-qgc-20260905T172856Z-51520`：258,942 bytes，SHA-256
  `FE45B4DCAAD69AEFA7871609A58D33723547C1B46B5A708DBF1A00E495DC59E0`；900/900 frame/type23/type24，
  831/831 range/geolocation status，112 个合格坐标，0 transport error，最大端到端 ACK 609 ms。
- 原生 QGC 点击第一次命中，相关目标 `flame` 进入 TRK；native QQuickItem pixels、transport-bound
  visible surface 和 toolbar smoke 均通过。VTOL Mission 最大离家 `134.918 m`、相对高度 `24.116 m`，
  MC/FW/MC、land/disarm 完整；容器移除、9 个测试端口释放、14550 不变。
- portable EXE 为 46,579,712 bytes，SHA-256
  `FFCC4FCF45EAE6E7A52ED6B2585BCCF29E5F36776D03D2EFA7F6527D1A96255B`。
- 当前 HEAD 全仓 3,317 tests：`3316 passed, 1 skipped, 0 failed, 0 errors`；JUnit 482,309 bytes，
  SHA-256 `67CBBE1C25256FA4619B375819748BFD9605CD4A2BC8F898CB7D7042DF5DE203`；Ruff 全仓通过。
- 以上仍是 dirty-worktree、current-parity 的 software/SIM 机器证据，不声明人工视觉、真实硬件、
  室外 GNSS/RTK、HIL/REAL 或实飞；最终 `v0.3.0` 继续阻断。

## v69 本轮已收口

- independent combined verifier 新增 `camera_info_intrinsics_semantics_validated`、
  `sensor_frames_complete`、`odometry_frames_complete` 三个硬检查；旧 pending/missing/tampered 结构均失败。
- combined runner 在写最终 source 前也强制 exact CameraInfo semantic status，防止 runner 根门掩盖传感器
  语义缺失；E7 fixture 已升级到同一契约。
- 当前权威 source `combined-sim-qgc-20260905T152520Z-75420` 来自干净 `46920e1`：900/900 sensor、
  RTSP、type-23、post-adoption type-24、full odometry；6 张 stale dynamic 图像被拒绝，0 张旧帧发出。
- source 255,256 bytes / SHA-256
  `9CAB1BC405571C2F287D1545D77B7EA1F7C4A56CC163AB76E1402CB362DB121B`；增强 verifier 2,139 bytes /
  SHA-256 `1EEFD154DF164F307D62EF1CFFCC7479E343B8E3FC6A7017A9BBE61EB19502D0`，44/44 checks 通过。
- Mission 最大水平距离 `130.616 m`、最大相对高度 `23.857 m`；PiP 第 1 次精确命中；容器移除、
  9 个动态端口释放、14550 不变。
- 当前 HEAD 全仓 3,309 tests：`3308 passed, 1 skipped, 0 failed, 0 errors`；JUnit 481,013 bytes、
  SHA-256 `3AF69527D05CCC985502C195D09DDE44793D7280111B04410B8B2DCB3C3661C6`；ruff 全仓通过。
- 最终版本仍阻断：四模式人工视觉 0/4、HIL/REAL、真实硬件、室外 GNSS/RTK 和实飞均未完成。

## v68 本轮已收口

- `gazebo_sensor_stream.py` 对实际 `CameraInfo` 的 width/height、K/P/R、distortion model 与 D 执行
  fail-closed 语义验证；标准 pinhole 零位、R identity、零 stereo translation 与仓库标定任一不符即失败。
- `GazeboSimBackend`、CLI、standalone/combined runner 均要求显式 camera calibration。canonical 标定 hash
  进入 sensor binding identity，验证状态进入 runtime result 与 persisted frame-set source attribution。
- 真实三帧诊断 `gazebo-camera-semantics-20260905T150106Z.json` 证明 K/P/R/D；Gazebo float 序列化的
  最大焦距差约 `3.53e-5 px`，故 intrinsic 使用 `1e-4 px` 明确容差，R/D 保持 `1e-12`。
- QGC PiP 原生点击增加最多 8 次的有界精确重试；每次仍同时验证坐标、QGC PID/HWND ownership 和
  foreground。本轮真实运行 `pointer_position_attempts=1`，不是靠放宽 ownership 通过。
- 权威 source `combined-sim-qgc-20260905T150510Z-27780`：900/900 sensor、RTSP、signed type-23、
  post-adoption type-24 和 full odometry；7 张 stale dynamic 图像被拒绝，0 张 stale frame 被发出。
- source 256,194 bytes / SHA-256
  `E800968AB63BF847CA53BE7798EAD3B2501B731E958D76AB5E8EAC447A999471`；独立 verifier 41/41，
  verifier SHA-256 `3D58600C9B57ED981C651DE79CCA90F0FED275096E9FD5CA416B104016E51078`。
- 7-item Mission 完成 upload/readback/commit/START_PERMITTED 和 VTOL 全生命周期；最大水平距离
  `129.057 m`、最大相对高度 `22.082 m`。容器移除、9 个动态端口释放、14550 ownership 不变。
- 当前 HEAD 全仓 3,306 tests：`3305 passed, 1 skipped, 0 failed, 0 errors`；JUnit 480,349 bytes、
  SHA-256 `0414978C7C13B2726CCA9CB94153F018D5AC16B8A399748255E52F9C08DA64AA`；ruff 全仓通过。
- CameraInfo K/P/R/D 机器语义子门已关闭；目标测距/落图并未在本次 900 帧联合 run 中重新配置。
  四模式人工视觉仍为 0/4，HIL/REAL、硬件、室外 GNSS/RTK 和实飞继续阻断最终 `v0.3.0`。

## v67 本轮已收口

- `CameraInfo` 保持正时间戳、因果性和逐帧 width/height 绑定，但按 Harmonic 实际行为视为会话静态标定；
  NavSat/Odometry 的 1.5 秒动态时效限制没有放宽。
- 独立 worker 短暂错序时丢弃无法绑定新鲜动态遥测的 image；若 gap 持续，helper 仍由统一 deadline
  fail-closed。新增计数分别记录 stale dynamic/NavSat/Odometry rejection。
- 中间失败 source `combined-sim-qgc-20260905T141805Z-19928` 在 532 emitted frames 暴露 NavSat
  `7,000,000,000 ns` 落后，门正确失败、容器和端口清理完成；它保留为根因 provenance。
- 最终 source `combined-sim-qgc-20260905T142324Z-32372`：900/900 sensor、RTSP、signed type-23、
  post-adoption type-24；4 张 stale dynamic 图像安全丢弃，VTOL attached、7-item Mission、QGC toolbar、
  router 和 cleanup 全部通过。
- source 255,480 bytes / SHA-256
  `FD386165212A68C6E2D053A0D58B05D796565AB716CB088439F0A6756EDC1090`；独立 verifier 2,006 bytes /
  SHA-256 `C195FFC92E88F53C54C4AC07C62891BD3806C13E97B47692F895DB9988DEAD60`，41/41 checks 全绿。
- 四模式 machine-binding adapter 对该 source 的 SIM 绑定检查 19/19 通过；将同一 source 指定为 HIL 时
  只在 `runtime_mode_matches` 失败，跨模式重标负控有效。该检查没有创建人工 accepted record。
- 全仓 3,294 tests：`3293 passed, 1 skipped, 0 failed, 0 errors`；JUnit 478,731 bytes、SHA-256
  `8261544D868E9222A22D7E5EFE5E1F437B19ECEFF5DB1567ECC02C3C38F617CD`。
- 该机器门没有配置 target-geolocation acceptance，CameraInfo K/P/畸变语义仍 pending；四模式人工
  视觉、HIL/REAL、硬件、室外 GNSS/RTK 和实飞不因本轮结果提升。

## v66 本轮已收口

- `capture_qgc_native_style.ps1` 与仓库 JSON Schema 定义 schema v2 并强制显式 runtime mode；报告绑定
  run ID、实际 QGC
  EXE identity、canonical parity identity/五组摘要、exact PID/client rect、遮挡状态和 PNG identity。
- 单记录 verifier 使用 native-v2 / legacy-HIL 双适配器；新报告直接覆盖四模式，旧 PX4 event 仅允许
  HIL。mode/PID/rect/遮挡/PNG/EXE/parity/UI contract 任一不一致都会关闭记录。
- HIL acceptance wrapper 已传入明确的 `hil`、同一 run ID 和 canonical parity。当前仍未产生任何人工
  accepted record，因此 E7 仍为 0/4、32 blockers、最终 `v0.3.0` 继续阻断。
- 全仓 3,286 tests：`3285 passed, 1 skipped, 0 failed, 0 errors`；JUnit 477,631 bytes，SHA-256
  `15F37922F866E1FCFBEDE011A461185333E25244B70447629955363635356C78`。Ruff、compileall、schema、
  PowerShell parser 与 parity 重算通过；parity 文件前后同为
  `289C1A3DD638C2A503ABEA44EE3F87564BB38D1232FB1428E92EA004180D060B`。

## v65 本轮已收口

- 人工记录新增强制 `runtime_mode`，且必须与绑定 machine report 一致；旧 v61 图只属于 HIL，不能
  冒充 REPLAY/SIM/REAL。
- E7 manifest writer/evaluator/schema/CLI/wrapper 已接入 `qgc_operator_visual_acceptance` 四模式集合；
  每次评估会重新打开记录和全部源文件，复算 review digest、source identity 与 verifier 结果。
- 集合必须按 `replay/sim/hil/real` 覆盖 4/4；重复 mode、重复记录、复用 machine report 或相同
  screenshot、文件篡改和任一缺失均 fail-closed。当前 accepted records 为 0/4，因此集合
  `present=false/passed=false`，总发布门继续关闭。
- release-gate schema 改动使 `px4_mission_path_hash` 更新为
  `F0804FEB609A9CBBBCE6E3932ED77BD7852C9AF71BDB612A76BBD56EAD12247A`；matrix、campaign、external
  join/revalidation 与当前 E7 已从不可变源按新 parity 重建并独立复验通过。
- 全仓 3,282 tests：`3281 passed, 1 skipped, 0 failed, 0 errors`；JUnit 477,082 bytes，SHA-256
  `C893DDEDEE6A07F659F354C0C3DD13214DFA81014F08EEC380F977ED4C77EE7A`；Ruff、compileall、全部 JSON
  Schema、PowerShell parser、E7 wrapper 与 campaign/matrix verifier 通过。

## v64 本轮已收口

- 新增 `multidetect.qgc_visual_acceptance` 和 `multidetect-qgc-visual-review` CLI，绑定 v61 exact
  PID/HWND machine report、原始 PNG、实际 QGC EXE、当前 `ui_contract_hash` 与 geolocation 负控。
- 12 项人工 checklist 覆盖单一原生 surface、地图点、1σ 圆、经纬度、类别、距离、方位、更新时间、
  复制、快照、记录和只读声明；缺项、false 或非 accepted decision 均关闭 gate。
- 负控必须至少 5 帧均有检测、全部 range invalid、全部包含预期 fail-closed reason 且 0 个假坐标；
  当前 v58 负控 source-binding 预检为 5/5 检测、0/5 坐标。
- 新增严格 JSON Schema、内容完整性摘要、独立重开 verifier 和 7 项定向回归。全仓 3,276 tests 为
  `3275 passed, 1 skipped, 0 failed, 0 errors`；JUnit 476,289 bytes，SHA-256
  `CA7A4F84146518BE23B47F9DFB0375BE58F77EF01846829375BFC3A323FD8881`。当前没有 accepted 人工 record，
  因此 `qgc_manual_visual_acceptance=not_claimed`，E7 blocker 数不变。

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

`configs/release-parity.json`：7,007 bytes，文件 SHA-256
`4824DA3149980A24CF4A14D785E6708E65AAAE27E55CEE2079DC3C67B96A1E57`。

| 契约组 | 当前哈希 |
|---|---|
| UI contract | `e94a670ec2a79b8c6d7f196a9acb9cffc5d60e6a080f7def665df3b8a6753a66` |
| Core algorithm | `ed10276782f0ce5b84df09bde723737d553fe6166f8792c9c57aefa5239dc53f` |
| Task contract | `89507cabbc0b6f442a3e33dfe582093b0f23a7395462d0146007cd9081d5a0ad` |
| Safety policy | `d5b15bf45f8f55b484b6f43f6c624a2f9c6494245ef1a1cae55e0b1a4dfbc87e` |
| PX4 Mission path | `7cd5cd2b0599c81d14fa9fd6fba6c978645cde4ec38ce4f8bbd20e4c6b32402f` |

## 功能成熟度矩阵

| 子系统 | 已实现/已自动验证 | 当前有效边界 | 仍需完成 |
|---|---|---|---|
| 输入与运行模式 | 本地/RTSP/Replay/Gazebo；REPLAY/SIM/HIL/REAL profile 与五组 parity | 软件契约和模拟输入 | 四模式同屏人工验收与真实 profile 证据 |
| 感知与模型治理 | ONNX provider、manifest、raw/post-NMS、候选隔离、延迟/精度工具 | v56 COCO car 仅 quarantined SIM candidate | 部署域 fire/smoke/safety 模型、数据集、评审和阈值 |
| 多目标跟踪 | 多目标池、短时恢复、ReID、遮挡/丢失/重获、TRK/LCK/TGT | 自动化与软件/SITL | 真实复杂场景身份精度与 G20/QGC 触控验收 |
| 测距 | metric depth、相机地面射线、多源融合、DEM/GNSS、时间/运动/残差不确定性 | v59 实际 Gazebo 像素；三档坡高/距离；advisory-only | 真实外参与 outdoor survey |
| 目标落图 | WGS-84、1σ 圆、GPS/RANGE fail-closed、事件/证据存储 | 三正场景 15/15 落图；近地平线 0/5 假点；v61 exact PID/HWND 机器截图通过 | 人工视觉签字与真实 GNSS |
| MultiDetectGCS | 原生 Fly View 覆盖、状态、目标地图、operator protocol、portable build | 本机 portable 可运行；v61 签名持续刷新与单一原生 surface 机器采集通过 | 四模式人工视觉对等与 E7 绑定 |
| 数字孪生 | PX4/Gazebo、三机型、场景/地图/DEM、wire v3 covariance、故障、watchdog | 三机型外部 wire-v3 实采；x500 position response；三档定位 | estimator 非零 covariance；fixed-wing/VTOL 对等动态响应 |
| 任务与控制 | 平台无关 planner、PX4 Mission 编译/反馈、Mode 3 动态权限 | 测试/SIM/HIL 框架；物理输出默认锁定 | 六份 HIL/REAL×三机型 feedback 与真实飞控验收 |
| Jetson/Pixhawk/链路 | Jetson profile、RTSP bench、只读 MAVLink、V6X 参数备份、G20/GR01 认证协议 | 软件/localhost 与部分只读现场快照 | 实机 Jetson provider、相机、GR01、V6X 同时在线验收 |
| 载荷安全 | 独立授权、双反馈、惰性 HIL、超时/重放/状态不一致 fail-closed | 无物理输出的 HIL | 经评审真实 controller transport 与受控地面台架 |
| 发布门 | E7 schema、证据绑定、recompute、严格/报告模式 | 当前 32 blocker，发布拒绝 | 全部现场/模型/视觉/Mission/HIL/REAL 门关闭后才发 v0.3.0 |

## 本机可运行 QGC

当前 portable 主程序：

```text
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe
```

- 大小：`46,579,712` bytes
- SHA-256：`FFCC4FCF45EAE6E7A52ED6B2585BCCF29E5F36776D03D2EFA7F6527D1A96255B`
- 当前 portable 已包含 v61 之后的 QGC C++/QML 变更；v105 没有界面源码变化，因此没有重复构建。
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
使用 v64 checklist 生成 accepted record；独立 verifier 通过后再经 manifest 接线评审接入 E7。当前
负向 source-binding 预检保持 5/5 检测、0 假点。v67 combined-SIM source 已能被通用 adapter 识别为
真实 SIM runtime，但它的 toolbar/Mission 截图没有配置 target-geolocation acceptance，不能直接充当
包含地图点/1σ/证据卡片的人工记录；SIM 模式仍需专门运行该定位画面。

### P1：三机型动态对等（机器子门已完成）

fixed-wing、multirotor、VTOL 已完成同一持续目标刷新、authority/watchdog、MANUAL_CONTROL 与 PX4
reported-position 路径；v63 又完成每机型双重复与三故障 fail-closed。剩余工作属于人工、硬件和实飞门，
不能用 SIM campaign 替代。

### P2：部署域模型和 replay-set

使用 v107 intake 显式导入 day/night/haze/reflection/smoke-only/no-fire 真实录像，再用 v108 workspace 完成
独立人工标注与 review receipt；用 v109 runner 在该真实 reviewed bundle 上运行固定 fire/smoke 模型，并继续
运行 person/vehicle 安全模型，测量 per-class precision/recall/false alarms/misses/latency，完成独立评审后再改变
model status。当前只完成 runner 与 synthetic HIL runtime smoke，没有 field receipt 或部署域 accuracy 结论。

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
