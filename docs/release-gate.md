# v0.3.0 E7 发布门

## v155 当前原子观测重绑定（2026-09-07，`v0.3.0-pre.8`）

type 26 原子目标观测与 evidence v4 更新后，`release-parity.json`、external join/revalidation、三机型
`GLOBAL_POSITION_INT` response matrix 和 6 正例/9 故障 campaign 均按当前源重新派生。当前 E7 report 为
`release-gate-pre.8-current-20260906T192400Z-v6-bound-report.json`，SHA-256
`996F595DE8E07EB41F63A1EDB887637DC8ABB0B5ACD6A5197D597C549E93CCEF`；结果仍为
`gate_passed=false`、`release_allowed=false`、31 blockers。软件原子传输改进不会替代 replay-set 部署域精度、
真实 field link、四模式人工视觉/Mission、HIL/REAL 硬件或最终 release channel 门。

## v150 hash-anchored GR01 trust registry（2026-09-07，`v0.3.0-pre.8`）

先在受控机器准备 `principals.json`，其中至少各有一个 `source_operator` 与 `reviewer`；每项必须包含唯一的
`principal_id`、`identity`、`key_id`、`public_key_ed25519_hex`、`valid_from_utc`、`expires_at_utc`，未吊销项固定
`revoked=false` 且两个 revocation 字段为 `null`。创建 registry 后，必须通过与 session/review 文件不同的渠道分发其
文件 SHA-256：

```powershell
python -m pip install -e ".[security]"
python .\scripts\verify_gr01_review_trust_registry.py create `
  --principals-json D:\trust\principals.json `
  --registry-id 12345678-1234-5678-9234-567812345678 `
  --issued-at-utc 2026-09-07T00:00:00Z `
  --valid-from-utc 2026-09-07T00:00:00Z `
  --expires-at-utc 2027-09-07T00:00:00Z `
  --out D:\trust\gr01-review-trust-registry.json
$trustRegistrySha256 = (Get-FileHash -Algorithm SHA256 D:\trust\gr01-review-trust-registry.json).Hash.ToLowerInvariant()

$env:MULTIDETECT_GR01_OPERATOR_ED25519_SEED_HEX = '<64位operator私钥seed hex>'
.\scripts\run_gr01_release_session.ps1 `
  -HostAddress 192.168.144.20 -Port 14580 `
  -HardwareId GR01-OPERATOR-001 -ExpectedSourceCidr 192.168.144.0/24 `
  -SourceOperator 'operator-a' -SourceOperatorKeyId 'field-operator-a-2026'
```

成功后，把 session 目录、detached source attestation 与 registry 复制到第二机；inventory SHA-256 和 registry SHA-256
都必须保留独立渠道记录。第二机 reviewer seed 必须对应 registry 中 active/non-revoked 的 reviewer principal：

```powershell
$env:MULTIDETECT_GR01_REVIEWER_ED25519_SEED_HEX = '<另一把64位reviewer私钥seed hex>'
python .\scripts\verify_gr01_release_session_review.py review `
  --inventory D:\review\gr01-session\gr01-release-session-transfer-inventory.json `
  --source-attestation D:\review\sidecars\gr01-source-attestation.json `
  --expected-inventory-sha256 <独立渠道保存的inventory SHA-256> `
  --trust-registry D:\review\trust\gr01-review-trust-registry.json `
  --expected-trust-registry-sha256 <独立渠道保存的registry SHA-256> `
  --reviewer 'reviewer-b' --reviewer-key-id 'field-reviewer-b-2026' `
  --reviewer-signing-key-env MULTIDETECT_GR01_REVIEWER_ED25519_SEED_HEX `
  --destination-root D:\review\gr01-session `
  --transfer-receipt-out D:\review\receipts\gr01-transfer.json `
  --out D:\review\receipts\gr01-portable-review.json

python .\scripts\verify_gr01_release_session_review.py verify `
  D:\review\receipts\gr01-portable-review.json `
  --trust-registry D:\review\trust\gr01-review-trust-registry.json `
  --expected-trust-registry-sha256 <独立渠道保存的registry SHA-256>
```

review schema v5 不再接收裸 operator/reviewer 公钥作为 CLI 信任根。operator key 必须在 source attestation 和 review
时点都有效，reviewer key 必须在 review 时点有效；registry 过期、key 过期/吊销、identity/key/public-key mismatch 或
未来 source attestation 均拒绝。key possession 不等于真人认证；human identity claims 固定 false，且仍须完成其余
E7/HIL/REAL/实飞门。

## v148 reviewer-signed portable review（2026-09-06，`v0.3.0-pre.8`）

第二台机器必须安装 security extra，并在执行 review 前使用预登记的 reviewer Ed25519 key。seed 是 32-byte hex，
只通过环境变量传入；不能写进仓库、命令参数或 session 媒体：

```powershell
python -m pip install -e ".[security]"
$env:MULTIDETECT_GR01_REVIEWER_ED25519_SEED_HEX = '<64位私钥seed hex>'
$reviewerPublicKey = python -c "import os; from nacl.signing import SigningKey; n='MULTIDETECT_GR01_REVIEWER_ED25519_SEED_HEX'; print(bytes(SigningKey(bytes.fromhex(os.environ[n])).verify_key).hex())"
# 在执行 review 前，把 $reviewerPublicKey 与 reviewer/key ID 的关系登记给独立审核方。

python .\scripts\verify_gr01_release_session_review.py review `
  --inventory D:\review\gr01-session\gr01-release-session-transfer-inventory.json `
  --expected-inventory-sha256 <独立渠道保存的inventory SHA-256> `
  --reviewer 'reviewer-b' --reviewer-key-id 'field-reviewer-b-2026' `
  --reviewer-signing-key-env MULTIDETECT_GR01_REVIEWER_ED25519_SEED_HEX `
  --destination-root D:\review\gr01-session `
  --transfer-receipt-out D:\review\receipts\gr01-transfer.json `
  --out D:\review\receipts\gr01-portable-review.json

python .\scripts\verify_gr01_release_session_review.py verify `
  D:\review\receipts\gr01-portable-review.json `
  --expected-reviewer-public-key-hex <预登记的reviewer公钥hex>
```

`verify` 不相信 receipt 内嵌公钥，必须与包外预登记值一致后再验签。该签名证明 key possession 和 receipt 完整性；
不能在收到待验证 receipt 后才把其中的 key 当可信来源。公钥与真人身份的登记关系仍由现场组织负责，所以 receipt 固定
`reviewer_human_identity_verified=false`。

## v147 trusted transfer-inventory anchor（2026-09-06，`v0.3.0-pre.8`）

v146 的复制目录和 inventory 若一起被替换，单靠包内清单无法建立包外信任。v147 因此要求现场 snapshot 后把脚本打印的
`Portable review trusted inventory SHA-256` 通过与 session 媒体不同的渠道保存；第二机 review 必须显式提供该值：

```powershell
python .\scripts\verify_gr01_release_session_review.py review `
  --inventory D:\review\gr01-session\gr01-release-session-transfer-inventory.json `
  --expected-inventory-sha256 <独立渠道保存的64位SHA-256> `
  --reviewer 'reviewer-b' --reviewer-key-id 'field-reviewer-b-2026' `
  --reviewer-signing-key-env MULTIDETECT_GR01_REVIEWER_ED25519_SEED_HEX `
  --destination-root D:\review\gr01-session `
  --transfer-receipt-out D:\review\receipts\gr01-transfer.json `
  --out D:\review\receipts\gr01-portable-review.json
```

实际 inventory 文件哈希必须先匹配 expected 值；不匹配时两个输出均不创建。review receipt schema v2 绑定
expected/actual hash 与 match，`verify` 再重算。该锚必须独立保存才有意义；它本身不是签名或身份认证，也不重验现场
target environment、全部 E7 传递来源、网络或硬件。

## v146 portable final-session review（2026-09-06，`v0.3.0-pre.8`）

现场 `run_gr01_release_session.ps1` 在 final record 独立复核之后自动创建：

```text
gr01-release-session-transfer-inventory.json
```

把完整 session 目录复制到第二台机器后，在复制包之外保存两份 review 输出：

```powershell
python .\scripts\verify_gr01_release_session_review.py review `
  --inventory D:\review\gr01-session\gr01-release-session-transfer-inventory.json `
  --expected-inventory-sha256 <独立渠道保存的64位SHA-256> `
  --reviewer 'reviewer-b' --reviewer-key-id 'field-reviewer-b-2026' `
  --reviewer-signing-key-env MULTIDETECT_GR01_REVIEWER_ED25519_SEED_HEX `
  --destination-root D:\review\gr01-session `
  --transfer-receipt-out D:\review\receipts\gr01-transfer.json `
  --out D:\review\receipts\gr01-portable-review.json

python .\scripts\verify_gr01_release_session_review.py verify `
  D:\review\receipts\gr01-portable-review.json `
  --expected-reviewer-public-key-hex <预登记的reviewer公钥hex>
```

`review` 先验证 6 个源文件逐字节不变，再离线重算五源 record、portable handoff、GR01 bench、配置一致性和
manifest/report GR01 绑定。它有意不读取原目标机 absolute paths/secrets，也不重开 E7 的全部传递来源，所以 receipt
固定声明 `target_environment_reverified=false` 与 `canonical_e7_sources_recomputed=false`。当前只有软件复制 fixture，
真实 GR01 session、真实第二机审阅和人工签字仍待完成。

## v145 imported source-plan pre-network match（2026-09-06，`v0.3.0-pre.8`）

开发机先用 `-PlanOnly` 生成 source plan，复制到现场机后可直接导入：

```powershell
.\scripts\run_gr01_release_session.ps1 `
  -HostAddress 192.168.144.20 -Port 14580 `
  -HardwareId GR01-OPERATOR-001 -ExpectedSourceCidr 192.168.144.0/24 `
  -TargetHostId FIELD-GCS-01 -SourcePlan .\incoming\gr01-source-plan.json
```

producer 会把输入逐字复制到 session，校验前后 SHA-256，创建并严格复验 target handoff，然后执行 `match`。
`match` 对账 source plan 与现场 CLI 的 endpoint、hardware ID、CIDR、阈值、secret names 和 configuration digest；成功后
才允许进入 bench/UDP。当前 clean smoke 已证明端口漂移在无 bench 文件时 exit 1，原参数 match exit 0；未执行真实网络。

## v144 five-source GR01 session gate（2026-09-06，`v0.3.0-pre.8`）

最终 `gr01-release-session.json` 已升级到 schema v3，必须同时绑定 plan、target handoff、hardware bench、E7 manifest
和 E7 report。现场 producer 的顺序固定为：

```text
plan create/verify
  → target handoff create/verify --check-environment --require-ready
  → hardware GR01 bench + independent verify
  → freshness-bound E7
  → five-source final record create/verify
```

正常现场入口增加了可审计的目标机身份：

```powershell
.\scripts\run_gr01_release_session.ps1 `
  -HostAddress 192.168.144.20 -Port 14580 `
  -HardwareId GR01-OPERATOR-001 `
  -ExpectedSourceCidr 192.168.144.0/24 `
  -TargetHostId FIELD-GCS-01
```

两个 secret 仍只从环境读取且不落盘。consumer 允许 origin plan 保存不同绝对路径甚至 origin secret presence 为 false，
但目标 handoff 必须在当前 target checkout/source/parity/tools/environment 上 ready；随后还会核对同一 plan 与 bench/E7
配置。当前仅完成无网络 handoff smoke 和软件 relocated-origin 五源 fixture；没有真实 GR01 session 或硬件通过声明。

## v143 portable GR01 plan handoff（2026-09-06，`v0.3.0-pre.8`）

source plan 可以随部署包复制到另一台 Windows 现场机；目标机 checkout、Python 和 output 的绝对路径允许变化，
但 Git HEAD、tracked-clean、canonical release parity 及 plan/producer/verifier/CLI 工具身份必须完全一致：

```powershell
python .\scripts\verify_gr01_release_session_handoff.py create `
  --source-plan D:\handoff\gr01-release-session-plan.json `
  --repository-root D:\Projects\Multi-Detect `
  --python D:\Projects\Multi-Detect\ground-control\MultiDetectGCS\.venv\Scripts\python.exe `
  --output-root D:\Projects\Multi-Detect\artifacts\evaluation `
  --host-id FIELD-GCS-01 `
  --out D:\handoff\gr01-release-session-handoff.json

python .\scripts\verify_gr01_release_session_handoff.py verify `
  D:\handoff\gr01-release-session-handoff.json --check-environment --require-ready
```

`create` 总能保存可诊断回执；只有来源完全等价且两个现场 secret 当前存在时 `ready_to_execute=true`。
第二条严格命令才是执行前门，non-ready 时 exit 1；结构/来源非法时 exit 2。回执只记录 secret 名称和 presence，
不保存值。当前 clean smoke 是 source-equivalent 但 non-ready/no-network；v144 已要求 final session 绑定 ready handoff，
但当前仍没有 GR01、E7、硬件或飞行通过。

## v142 execution-plan-bound GR01 session（2026-09-06，`v0.3.0-pre.8`）

最终 `gr01-release-session.json` schema v2 现在绑定四份来源：source-bound plan、hardware bench、E7 manifest、
E7 report。consumer 不只比较 plan 文件 SHA，还会重新运行 plan verifier，并逐项证明计划中的 endpoint、GR01
hardware ID、source CIDR、minimum round trips、loss/P95/retry 门限和 evidence max-age 与实际 bench/E7 相同。

producer 在 bench 前创建并严格验证 plan，最终 record create 又显式传入同一 `$planPath`；因此换 plan、改工具、
修改 plan 参数、替换 bench 或 E7 来源都会失败。当前无现场 secret，干净源码 smoke 仍是 non-ready/no-network，
所以 E7 保持 `false / false / 32 blockers` 且 `real_gr01_operator_link.present=false/passed=false`。

## v141 source-bound GR01 plan gate（2026-09-06，`v0.3.0-pre.8`）

plan schema v2 不再只证明“参数看起来有效”，还绑定执行源码：Git HEAD、tracked-clean、canonical
`release-parity.json` 的 bytes/SHA 与来源复算结果，以及 PowerShell producer、repository CLI shim、独立 verifier、
plan module 四个工具的 bytes/SHA。独立 verifier 会重开这些文件；`--check-environment` 还会核对 secret presence
是否从计划生成后发生变化，`--require-ready` 拒绝任何 diagnostic/non-ready plan。

实际 `run_gr01_release_session.ps1` 在打开 UDP 前必须创建 plan 并通过上述两个开关。dirty tree、stale parity、
tool drift 或 missing secret 均不会进入 bench。提交 `abca8465d53ea525b545380ff06e40ca922a81da` 的无网络 smoke
确认 source identity 全部通过，但两个现场 secret 不存在，故 `ready_to_execute=false`；这不是 GR01/E7/hardware pass。

v141 parity 重算后的 external join/revalidation、reported-position matrix/campaign 均通过；E7 报告仍为
`gate_passed=false`、`release_allowed=false`、32 blockers，`real_gr01_operator_link.present=false/passed=false`。

## v140 no-network GR01 session rehearsal（2026-09-06，`v0.3.0-pre.8`）

到现场前可先运行同一 producer 的预演模式：

```powershell
.\scripts\run_gr01_release_session.ps1 `
  -HostAddress 192.168.144.20 -Port 14580 `
  -HardwareId GR01-OPERATOR-001 `
  -ExpectedSourceCidr 192.168.144.0/24 `
  -PlanOnly `
  -PlanOutput artifacts\evaluation\gr01-session-plan.json
```

它只验证配置与工具链，不运行 bench，不打开 UDP，不观察硬件。回执只保存 secret 环境变量名和 `present`，
从不保存值；缺失时 `ready_to_execute=false` 并列出原因。closed schema 固定 `network_contacted=false`、
`hardware_observed=false` 及全部 no-control claim，所以 plan 不能作为 GR01/E7/hardware pass 使用。

## v139 transactional GR01 bench-to-E7 session（2026-09-06，`v0.3.0-pre.8`）

现场不再需要手工拼接“bench 文件 → E7 参数 → report”三步。新 wrapper 在唯一 write-new 目录中完成：

```powershell
$env:MULTIDETECT_OPERATOR_HMAC_KEY = '<FIELD-SECRET>'
$env:MULTIDETECT_MAVLINK_SIGNING_KEY_HEX = '<64-HEX-FIELD-KEY>'
.\scripts\run_gr01_release_session.ps1 `
  -HostAddress 192.168.144.20 `
  -Port 14580 `
  -HardwareId GR01-OPERATOR-001 `
  -ExpectedSourceCidr 192.168.144.0/24 `
  -MinimumRoundTrips 100 `
  -MaximumPacketLossRate 0.01 `
  -MaximumAckLatencyP95Milliseconds 500 `
  -MaximumEvidenceAgeSeconds 900
```

执行顺序固定为 hardware bench、`verify_gr01_link_bench.py --require-passed`、current E7 wrapper、
`gr01_release_session.py create`、同 consumer 的 `verify`。consumer 不信任 saved report：它使用 report 内的
`evaluated_at_utc` 重新运行 canonical evaluator，并逐项核对 bench/manifest/report 的 SHA、bytes、GR01 子门和
freshness。最终 `gr01-release-session.json` 只在全部步骤通过后原子生成且拒绝覆盖。

两个 secret 的值只从环境读取，不进入命令参数和记录；session record 固定
`hardware_flight_acceptance=not_claimed` 且 flight/payload/physical-release 全为 false。它只封闭 GR01 E7 子门，
不能把整体 `release_allowed`、HIL/REAL、实飞或载荷状态自动升级。当前没有执行真实现场 session。

## v138 REAL GR01 evidence freshness（2026-09-06，`v0.3.0-pre.8`）

GR01 选择/ACK 台架报告不能作为永久凭证。新 manifest 同时绑定源报告的 `observed_at_utc`、等价
`report_timestamp_epoch_s` 和 `max_age_s`；默认有效期 900 秒。writer 在创建 manifest 当下检查时区、有限值、
未来偏差和 age，过期报告不会进入新 manifest。evaluator 又在每次复评时使用顶层 canonical
`evaluated_at_utc` 重算 age，因此一个创建时合格的报告会在窗口结束后自动关门。

历史 prerelease manifest 若完全不含 freshness 三字段仍可审计读取；任一字段单独出现属于 partial binding，
立即失败。最终 `0.3.0` 即使源 hash、硬件 ID、endpoint/source route、ACK 和签名全部通过，也必须包含三字段，
否则增加 `final release requires freshness-bound REAL GR01 operator-link provenance` 阻断。

```powershell
.\scripts\run_current_release_gate_pre8.ps1 `
  -RealGr01OperatorLinkBench D:\FieldEvidence\gr01-link-bench.json `
  -RealGr01OperatorLinkMaxAgeSeconds 900 `
  -OutputDirectory D:\FieldEvidence\release-gate `
  -Stamp 20260906T120000Z
```

部署身份不是单个字符串，而是同一 hash-bound report 内的 `hardware_id + remote_host:remote_port +
expected_source_cidr + local_source_address + observed_at_utc`。当前没有真实报告，E7 仍显示
`present=false/passed=false`，不构成 GR01/G20、HIL、REAL、射频或实飞通过。

## v137 REAL GR01 operator-link 独立门（2026-09-06，`v0.3.0-pre.8`）

目标选择/ACK 与 fire-alert 是两条用途不同的数据链。新 manifest 顶层键
`real_gr01_operator_link` 只接受 canonical `gr01-link-bench` verifier 已确认通过的硬件报告；writer 固定
要求 `event=gr01_bench_passed`、GR01、non-loopback endpoint、source route、双向 ACK、application HMAC、
MAVLink2 signature 全部为真，四个控制/投放位全部为假，并保存源报告 SHA-256 与字节数。

evaluator 不信任 manifest 中的布尔投影：它重新打开源 JSON、核对 hash/bytes、重跑 verifier，再逐项比较
endpoint、source CIDR/address、round-trip/timeout/attempt、丢包、时延、checks、thresholds、签名和 no-control
字段。未知字段、源文件替换、manifest 投影篡改或 software baseline 都会阻断。最终 release channel 缺该记录时
明确报告 `final release requires a hash-bound REAL GR01 operator selection/ACK bench`；已有
`real_alert_link` 也不能抵消这一阻断。

当前 wrapper 用法：

```powershell
.\scripts\run_current_release_gate_pre8.ps1 `
  -RealGr01OperatorLinkBench D:\FieldEvidence\gr01-link-bench.json `
  -OutputDirectory D:\FieldEvidence\release-gate `
  -Stamp 20260906T120000Z
```

输入为空时 prerelease report 仍可生成并明确显示 `present=false/passed=false`；只有切换到最终 `0.3.0`
release channel 时该缺失项才成为新增 final-only blocker。当前 v137 report 因既有现场/人工门保持 32 blockers，
并未形成 GR01、HIL、REAL、射频或实飞通过声明。

## v94 sealed single-leaf field session（2026-09-06，`v0.3.0-pre.8`）

现场每叶统一入口：

```powershell
python -m multidetect.field_covariance_session run-next `
  --receipt D:\FieldEvidence\receipts\covariance-workspace-receipt.json `
  --journal-dir D:\FieldEvidence\session-journal
```

该入口在一次进程中完成 receipt/workspace/parity 复验、next-leaf 选择、source preflight、online probe、
probe write-new、磁盘独立复验、capture subprocess、正式 report 复验和成功 session seal。online probe 未
通过时 `capture_started=false`，只留下 `.online-preflight.json` 诊断；不存在成功 `.session.json`。

成功记录复验：

```powershell
python -m multidetect.field_covariance_session verify-record `
  --record D:\FieldEvidence\session-journal\<leaf>.session.json
```

归档 verifier 允许被采集叶不再是 `next_leaf`，否则第一个成功 capture 会令自身 probe 无法事后审计；但它
仍重算 probe schema/digest/source/parity/mode binding/identity/query budget，要求当前 campaign 中该叶及
此前所有叶报告继续正式通过。record 的进度必须严格是 `ordinal/6`，所有 acceptance claims 固定为 false。
六份正式 report 仍需由 E7 六叶 builder 独立打开；session record 不替代 E7 evidence。

portable workspace 生命周期现在分两层：未接收的移动介质和 receive 输入必须精确 20 文件；接收后的本机
workspace 可出现且只可出现 campaign 声明的六个 report，且每个存在的 report 必须通过正式 verifier。

## v93 transactional workspace receive（2026-09-06，`v0.3.0-pre.8`）

第二台机器从只读/移动介质接收时执行：

```powershell
python -m multidetect.field_covariance_workspace receive `
  --bundle E:\covariance-workspace `
  --out D:\FieldEvidence\covariance-workspace `
  --receipt D:\FieldEvidence\receipts\covariance-workspace-receipt.json
```

接收器不使用普通目录复制作为成功判据。它先运行 v92 verifier，然后只复制闭合集中的 20 个文件到目标
同盘临时目录，对副本再次运行 workspace verifier 和六叶 preflight，最后原子 rename。目标和回执均是
write-new；回执必须位于源/目标 bundle 外，回执落盘失败会回滚刚创建的目标。

拔除源介质后执行：

```powershell
python -m multidetect.field_covariance_workspace verify-receipt `
  --receipt D:\FieldEvidence\receipts\covariance-workspace-receipt.json
```

该命令重算 receipt digest，按绝对本机 destination 重验 20 文件、workspace digest、当前 release parity、
campaign 和 source preflight，并核对下一条 argv 与 false claims。它不要求源介质仍在线，也不观察
Pixhawk；所以 receipt 不是 E7 叶证据。只有随后本机 probe/capture 产生并复验的六份 covariance report
才能进入 E7。

## v92 portable covariance workspace（2026-09-06，`v0.3.0-pre.8`）

第一台准备机导出：

```powershell
python -m multidetect.field_covariance_workspace export `
  --config .\configs\pixhawk-covariance-field-campaign.json `
  --out .\artifacts\handoff\covariance-workspace
```

第二台现场机复制整个目录后验证：

```powershell
python -m multidetect.field_covariance_workspace verify `
  --bundle D:\FieldEvidence\covariance-workspace
```

只有 `gate_passed=true` 时，才使用返回的绝对 `campaign_path` 运行本机 `field_covariance_campaign
preflight`。bundle verifier 要求与接收机当前 `release-parity.json` 完全一致，重开 18 份源并运行既有
六叶 preflight；目录内只能存在 manifest、campaign 和 18 份已绑定源。

该 bundle 解决文件系统路径搬迁，不迁移连接状态：endpoint/baud 和 signed profile 身份原样保留，online
preflight 不进入 bundle。第二机仍须依次执行 `probe-next` 和 `capture-next`，从本机当前 Pixhawk 重新取得
HEARTBEAT、firmware、link、qualification 与 covariance。bundle claims 全为 false，不直接进入 E7。

## v91 sealed online preflight（2026-09-06，`v0.3.0-pre.8`）

`probe-next --status-out` 现在保存 schema-v2 operational observation。正式采集前在同一 campaign
workspace 立即独立复验：

```powershell
python -m multidetect.field_covariance_campaign verify-probe `
  --config .\configs\pixhawk-covariance-field-campaign.json `
  --probe-report .\artifacts\evaluation\field-covariance\online-preflight.json
```

默认 freshness 窗口为 300 秒；也可显式传入 `--maximum-age-seconds 120`。verifier 不信任保存的摘要，
会重开 campaign/Mission/feedback/profile，重新执行六叶静态 preflight 和 authoritative mode binding，
核对 leaf 仍是当前 `next_leaf`，并重算采样时序、identity response、查询/TX 预算、reasons、gate、
canonical digest 和当前 release parity。任一漂移、过期或 symlink 返回非零。

这仍不是 E7 covariance leaf：它不含 estimator covariance 样本，所有 acceptance claims 固定为 false，
也不能跨 workspace 搬运后冒充复验。正式 `capture-next` 自己会重新在线探测，不依赖旧文件放行。

## v90 当前 Pixhawk identity probe（2026-09-06，`v0.3.0-pre.8`）

六叶 source READY 后、正式采集前运行：

```powershell
python -m multidetect.field_covariance_campaign probe-next `
  --config .\configs\pixhawk-covariance-field-campaign.json `
  --probe-samples 100 `
  --probe-interval-seconds 0.1 `
  --status-out .\artifacts\evaluation\field-covariance\online-preflight.json
```

通过条件包括当前 resolved endpoint、HEARTBEAT、firmware identity、transport/qualified link 和
operational qualification 与 feedback/profile 完全一致。probe 不配置 ranging stream；唯一允许的发送是
最多三次 firmware identity 查询，且 `messages_transmitted == attempts`。该状态文件可以覆盖更新，不是
release evidence。正式 `capture-next` 会再次探测，失败时不启动 covariance capture subprocess。

## v89 六叶采集 source preflight（2026-09-06，`v0.3.0-pre.8`）

正式连接 Pixhawk 前先运行无 I/O 静态预检：

```powershell
python -m multidetect.field_covariance_campaign preflight `
  --config .\configs\pixhawk-covariance-field-campaign.json `
  --status-out .\artifacts\evaluation\field-covariance\source-preflight.json
```

输出必须为 `ready_count=6`、`blocked_count=0`、`gate_passed=true`。预检会重开全部 Mission、feedback
和 runtime profile，并核对 adapter、mode、endpoint/baud、签名/profile gate、只读零发送和 output
lock；它不会打开串口或发送 MAVLink。`capture-next` 内部会再次运行同一预检，任一叶 BLOCKED 时不会
启动采集 subprocess。该结果是可更新的操作状态，不是 release evidence，也不证明当前飞控在线身份；
HEARTBEAT、firmware 与 current-link 仍由正式 covariance capture 现场验证。

## v88 六叶现场采集 runner（2026-09-06，`v0.3.0-pre.8`）

先复制并填写 `configs/pixhawk-covariance-field-campaign.example.json`。其中每个叶必须指向已经生成并
验证过的 executable Mission、PX4 Mission feedback v2、signed runtime profile，以及本叶唯一的
write-new report path。依次运行：

```powershell
python -m multidetect.field_covariance_campaign status `
  --config .\configs\pixhawk-covariance-field-campaign.json

python -m multidetect.field_covariance_campaign preflight `
  --config .\configs\pixhawk-covariance-field-campaign.json

python -m multidetect.field_covariance_campaign capture-next `
  --config .\configs\pixhawk-covariance-field-campaign.json `
  --dry-run

python -m multidetect.field_covariance_campaign capture-next `
  --config .\configs\pixhawk-covariance-field-campaign.json `
  --status-out .\artifacts\evaluation\field-covariance\status.json

python -m multidetect.field_covariance_campaign verify `
  --config .\configs\pixhawk-covariance-field-campaign.json `
  --binding-out .\artifacts\evaluation\field-covariance\matrix-binding.json
```

每次正式 `capture-next` 前必须先看 `next_leaf`，连接对应机型并确认 endpoint。runner 不自动切换飞控，
不启用 hardware control，一次只采一个叶。已有失败报告会保持原样并阻止继续；检查原因后应人工归档
失败文件，再重新运行，而不是原地覆盖证据。六叶完成后输出的 `e7_arguments` 可直接附加到 manifest CLI。

## v87 HIL/REAL covariance 六叶发布矩阵（2026-09-06，`v0.3.0-pre.8`）

E7 顶层新增 `pixhawk_covariance`。CLI 使用六次
`--pixhawk-covariance-evidence MODE VEHICLE REPORT`，组合必须严格为 HIL/REAL × fixed-wing、
multirotor、VTOL，且六份 report 路径互异。writer 与 evaluator 都会重新运行 v86 schema-v3 verifier，
再核对每叶嵌入的 Mission、Mission feedback、signed runtime profile、endpoint、baud、HEARTBEAT、
firmware identity、当前 transport/qualified link、read-only 与 hardware-control-disabled 状态。

该矩阵还必须匹配四模式 E7 的 `px4_mission_path_hash`。预发布清单允许字段缺省，以兼容历史诊断；字段
一旦出现便是 authoritative，任一叶失败会关闭总门。最终 release 无条件要求完整矩阵。当前只有外部
PX4/Gazebo SIM 正控，没有伪造 HIL/REAL 六叶，因此这轮只关闭软件聚合缺口，最终发布状态不变。

## v86 strict covariance evidence contract（2026-09-06，`v0.3.0-pre.8`）

covariance observation 现在必须同时通过 Draft 2020-12 schema v3、canonical evidence hash、当前
Schema/script/provider/parity source binding 和 raw diagnostics/records 派生重建。Schema 与所有关键
嵌套对象均关闭未知字段；失败采集仍能以显式 null/空数组形成可复核证据，但不能形成通过 claim。
Schema 自身加入 `px4_mission_path_hash`，因此 REPLAY/SIM/HIL/REAL 不能在保持旧 parity 身份时漂移
现场 covariance 格式。该子门不替代真实 HIL/REAL、硬件、室外 GNSS、人工视觉或实飞验收。

## v85 current-link covariance 子门（2026-09-06，`v0.3.0-pre.8`）

PX4 estimator covariance 子门现在除消息数量、raw covariance、来源、只读发送预算与 firmware hash
外，还要求结束快照的 transport link 当前健康、operational qualification 当前通过且无 reasons，
以及 firmware response age 落在本次采集时长内。HIL/REAL mode binding 也重复执行同组 provider
健康检查，不能靠外层报告标签绕过。当前 PX4 SITL 证据三项均通过并由 verifier 重建；它只关闭
SIM 下的当前链路证据缺口，不提升 HIL/REAL、硬件、室外 GNSS、人工视觉或实飞状态。

## v84 PX4 firmware identity availability 子门（2026-09-06，`v0.3.0-pre.8`）

现场 covariance 门所需的 `AUTOPILOT_VERSION` 不再依赖 PX4 偶然广播。provider 只在已绑定 autopilot
HEARTBEAT 后有界发送 message-148 读取请求；证据必须证明 1–3 次请求内收到合法、source-bound、匿名化
firmware identity。请求预算、响应状态与总发送计数均可重建，三次无响应或 hash 非法时不产生任何
SIM/HIL/REAL covariance claim。

当前外部 PX4/Gazebo source 为
`artifacts/evaluation/terminal-continuation-v84/px4-sitl-version-covariance-v84-pre8-final.json`：
81,564 bytes，文件 SHA-256 `E825EADA70107318C1D78A9DF138AFA82D227D7659FEE1B48A0A5A03F9ACB29E`，
内部 evidence SHA-256 `F2098EC5625EBB4F9E9BC2FAC4E4AC45188C926CCCC6C47022AB1A098C09BD77`。
207/207 条 ODOMETRY/covariance、112 份记录通过；一次 identity 请求得到响应，三轮七消息 stream 配置
加一次读取的总发送数为 22。该 SIM 正控不替代真实 HIL/REAL、硬件或实飞门。

## v83 HIL/REAL covariance identity 子门（2026-09-06，`v0.3.0-pre.8`）

HIL/REAL covariance capture 现在要求可执行 Mission、PX4 Mission feedback v2 和签名 runtime profile
三份来源，并在采集与复验时重开它们。当前 endpoint/baud、HEARTBEAT 五元身份、firmware identity、
只读 provider、hardware control disabled、runtime output lock 与 flight control disabled 必须同时满足；
任何缺失或漂移都会拒绝 `hil_covariance_observation` / `real_covariance_observation` claim。此 claim 只表示
在已绑定现场会话观察到 estimator covariance，不等于完整 HIL/REAL、硬件控制或实飞 acceptance。

当前仅完成外部 PX4/Gazebo SIM v2 正控：110/110 条 ODOMETRY/covariance、83 份记录，报告文件
61,131 bytes、SHA-256 `B3DA32BF0C1A1C0C6244C9F5A9888774AA205C4701B73FCAB490BFD4F1EF40BE`，
内部 evidence SHA-256 `306E30B25C93EA4D642ECEA0515F79DEC2DF599C5DB8C884B50F35C289C08112`；
容器已删除。HIL/REAL 工具门已实现但尚无实机正证据，E7 的现场 blocker 不变。

## v82 PX4 estimator covariance SIM 子门（2026-09-06，`v0.3.0-pre.8`）

共享只读 Pixhawk ingress 已消费 MAVLink `ODOMETRY.pose_covariance`：source system/component、parent/child
frame、非回退时间和 250 ms attitude association 全部 fail-closed；21 项 upper triangle 被还原为
roll/pitch/yaw 3×3 covariance，并验证 finite、正方差和 PSD。配置 floor 只在真实 sigma 派生后应用，
来源明确标记；无效新消息不会继续暴露旧 uncertainty。采集器只发送
`MAV_CMD_SET_MESSAGE_INTERVAL`，没有 mission、mode、parameter-write 或 actuator 指令。

pinned external PX4/Gazebo SIM 观察到 64/64 条非零 estimator covariance，保存 47 条变化记录，
重建 verifier 和全部 checks 通过，容器已清理。证据位于
`artifacts/evaluation/terminal-continuation-v82/px4-sitl-odometry-covariance-v82-pre8-final.json`，本地文件
SHA-256 为 `35a66193...0fbd5`，canonical evidence SHA-256 为 `14c75403...202a2`。该 ignored local
artifact 不在 Git 分发范围内。

这证明 SIM 上的实际 PX4 estimator covariance 可以进入同一共享算法，不构成 HIL/REAL、真实 Pixhawk、
室外 GNSS、三机型硬件响应、人工 QGC 或实飞验收；E7 仍有 32 blockers。v82 全仓回归为
3,358 tests：`3357 passed, 1 skipped, 0 failed, 0 errors`，JUnit SHA-256 为
`e9306292...f81d9`。

## v81 三机型外部 covariance 证据子门（2026-09-06，`v0.3.0-pre.8`）

fixed-wing、multirotor、VTOL 各完成一次 pinned Docker/PX4/Gazebo 外部运行和 3 帧 `MDGZSIM3`
采集；runtime/sensor gates、逐帧重构与容器清理全部通过。manifest 绑定 covariance convention、记录数、
非零记录数、派生 sigma 持久化、floor 与来源。三份 source 的原始 covariance 都是全零矩阵，故最终
sigma 来自显式 `0.3°/0.3°/1.0°` floor；系统没有声称收到 estimator uncertainty。

该软件/SIM 子门不减少 E7 的 32 个 blocker；模型精度、人工 QGC、室外 GNSS、HIL/REAL、真实硬件、
Mission feedback 六叶矩阵与实飞仍须各自通过。

## v80 Gazebo 姿态 covariance 子门（2026-09-06，`v0.3.0-pre.8`）

`MDGZSIM3` 已将 Gazebo `PoseWithCovariance` 的 angular 3×3 block 纳入 276-byte 固定 header、整帧
SHA-256、逐帧 manifest 和独立重放。双端验证 finite/对称/PSD，backend 把 fixed-axis X/Y/Z covariance
经数值 Jacobian 转成 NED/FRD roll/pitch/heading `1σ`，再应用显式 floor；奇异姿态使用保守预算。
producer/backend 同时受 `core_algorithm_hash` 约束。

这关闭的是软件协议与来源绑定缺口。v81 已补跑外部 Docker/PX4/Gazebo v3 采集，既有 v2
外部证据只保留历史价值；E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，模型、
人工 QGC、室外 GNSS、HIL/REAL、真实硬件和实飞门均不变化。

## v79 姿态不确定性 provenance 子门（2026-09-06，`v0.3.0-pre.8`）

共享测距输出现在绑定实际使用的 roll/pitch/heading `1σ` 与来源；完整 telemetry tuple 优先，整组缺失
才走带名称的配置 fallback，部分或无来源输入 fail-closed。这关闭了“算法传播 sigma、运行时却无法证明
sigma 从哪来”的软件证据缺口，但没有关闭现场精度门：MAVLink ATTITUDE 当前不提供 estimator covariance，
Gazebo wire-v2 当时也尚未传 `OdometryWithCovariance.covariance`。因此该轮不减少 E7 的 32 个 blocker，
也不提升模型、人工 QGC、室外 GNSS、HIL/REAL、硬件或实飞声明。

## v63 PX4 response repeatability/fault campaign（2026-09-05，`v0.3.0-pre.8`）

E7 新增 hash-bound `px4_reported_position_response_campaign`：要求 fixed-wing、multirotor、VTOL 各两次
互不相同的正常 `GLOBAL_POSITION_INT` 响应，并要求每机型覆盖 target-refresh dropout、heartbeat
dropout、mode loss。当前 6 positive + 9 fault 全部通过 campaign verifier；负向运行的 Docker/PX4
runtime gate 均通过，对应飞行门全部 fail-closed。manifest/report 位于
`artifacts/evaluation/terminal-continuation-v63/release-gate-pre.8-current-20260905T133000Z-v6-bound*.json`。
该子门不改变 HIL/REAL、硬件、人工视觉、实飞和 prerelease 阻塞，总 blocker 仍为 32。

## v59 多工况目标定位矩阵子门（2026-09-05，`v0.3.0-pre.8`）

v58 新增、v59 关闭 software/SIM 工况覆盖门，但它尚未接入为可减少 E7 blocker 的通过输入。矩阵逐份
重放 schema-v2 Gazebo geolocation source，并同时满足：3 个/15 帧正样本、1 个/5 帧零坐标负样本、
3 个相互分离的高度档、3 个距离档和 2 个坡度档。当前报告含 3 个正向工况、15 帧和 1 个 5 帧
零坐标负控，所有 source 都独立重现，`gate_passed=true`。该通过结果尚未写入 E7 manifest/schema，
也没有 QGC 人工视觉或现场证据，不能替代最终版本统计。E7 仍为 32 blockers，最终 `v0.3.0` 保持阻断。

## v56 headless Gazebo camera 与 quarantined candidate 三控门（2026-09-05，`v0.3.0-pre.8`）

v56 修复 composite camera include 的 pose reference 和 Gazebo→wire 图像行序，并在 pinned disposable
PX4/Gazebo 上取得完整正立目标的实际 camera pixels。随后用推理前冻结、独立于模型且与 wire-v2 首帧
SHA-256 绑定的 annotation，运行同一 candidate session 的 present / blank / occluded 三控。

| 子门 | v56 结果 | 能得出的结论 |
|---|---|---|
| camera mount frame | 三机型 include/joint 都相对 `base_link` | x500 相机不再因 composite model origin 差异落到 terrain 以下 |
| pixel row order | source=`top_to_bottom`、wire=`top_to_bottom`、flip=`false` | 取消重复翻转；frame-set 的矛盾行序会在推理前失败 |
| actual Gazebo pixels | 3/3 份 1280×960 BGR24；v21 runtime SHA-256 `3FBF2BDE...2643D` | headless Gazebo camera 中目标完整、正立；容器 stop/rm 且无残留 |
| annotation isolation | frame SHA `3D1BA6BB...D933`；truth 未加载、annotation 不传给 detector | truth 与 runtime/inference 输入隔离；annotation 用于评分和 occlusion mask |
| present control | `car` × 1；confidence `0.8012787104`；IoU `0.8882733111` | 该 quarantined candidate 在这一帧满足 smoke 门限 |
| blank / occluded | 0 / 0 detections | 两个负控通过 |
| v22 candidate evidence | 5,908 bytes；SHA-256 `D48865EC9296C2778D7CF7D6C486D632410689EF1B23BAFC99BEE04A55365BC9`；exit `0` | 单帧三控 `gate_passed=true` |
| governance | `quarantined`、production=false、accuracy=false、flight/physical=false | 不允许由该 smoke 提升为生产模型或物理飞行能力 |
| focused regression | `326 passed`；Ruff/compileall pass | 本轮 camera/frame-set/evaluator 契约通过 |
| three-airframe binding | 2,371 bytes；SHA-256 `7DAB627CC1211EFDD60A98C71575EE697E5062B9CB1020AB905B7A0CFFF896B2` | v54 stale hashes 被拒绝后，当前 fixed-wing/multirotor/VTOL dry-run preflight 重新绑定；不代表 external runtime |
| release parity | 5,098 bytes；SHA-256 `893E55ED8BDADE76DF85A5F50ADB3471D517237DE6F7B5F094C07AFE278C3397` | safety-policy hash 已更新，canonical document 与当前共享源码一致 |
| full strict regression | 3,199 tests：`3198 passed, 1 skipped, 0 failed, 0 errors`；JUnit `2799FCA4...CBBBC` | exit `0`；唯一跳过为 Windows symlink 权限项 |
| external source rebind | 旧 v52 join 对当前 parity 重算失败；新 join/revalidation `D5120A0F...4327F` / `AA925E65...6D46` | 六份既有 software-only source 与当前 parity 一致；不是新的 external runtime |
| current E7 report-only | exit `0`；manifest/report `94C38CF6...492D` / `AD14941F...0D87` | 诊断成功生成，但总门仍 false |
| current E7 strict | exit `2`；strict report `B4BE420E...87DF`；32 blockers | 发布按设计拒绝；v56 3-D/external 子门通过不等于最终 release |
| model review | absent | quarantined 单帧 candidate 未冒充生产审核或部署域 accuracy |

该子门不关闭 accuracy benchmark、共享 ranging→geolocation→map/evidence、三机型动态视觉或人工 QGC Fly
View，也不替代真实 Jetson/相机/Pixhawk、室外 GNSS、HIL、REAL 和实飞。QGC C++/QML 未改，本轮不生成
新 EXE。最终 `v0.3.0` 仍被这些门阻断。

当前 32 条 blocker 按责任域归并为：部署域 replay-set 的完整样本、精度与独立审核尚未通过；真实 field-link
报告超过 900 s freshness 且 `hardware_link_ready=false`；REPLAY/SIM/HIL/REAL 的人工 Fly View 与 Mission
path acceptance 未声明通过；HIL/REAL 的硬件与真实控制未声明通过；产品仍处于 `prerelease`，版本面尚未切换
到最终 `0.3.0`。因此 report-only 的退出 `0` 只表示报告成功生成，strict 的退出 `2` 才是当前发布判定。

## v55 一次性 PX4 SITL/Gazebo 模拟机体位置响应机器门（2026-09-05，`v0.3.0-pre.8`）

v55 在 x500 非平地 validation world 中显式执行：synthetic truth-free perception → public perception seam →
生产 AI executor → 共享 authority/watchdog → 既有 loopback MAVLink adapter → `MANUAL_CONTROL` → PX4
SITL/Gazebo → PX4 `GLOBAL_POSITION_INT`。`SET_MODE(POSCTL)` 与控制输出共用同一个 socket；只在 disposable
container 中设置 `COM_RC_IN_MODE=1`，且没有 Gazebo truth、`set_pose` 或 teleport。

| 子门 | 必须读取的字段 | v4 结果 / 可以得出的结论 |
|---|---|---|
| runtime | `runtime_gate_passed`、PX4 identity、telemetry qualified | `true`；hash-pinned disposable PX4/Gazebo 实际运行 |
| AI policy | `ai_policy_gate_passed`、accepted/rejected/failsafe/watchdog | `true`；557 / 0 / 0 / 0，生产 policy 与共享 adapter 被实际调用 |
| target policy | accepted TRACK 与 truth-free source | `true`；synthetic target 进入公开 perception seam，不代表 camera accuracy |
| current mode | SET_MODE sent、heartbeat armed/custom enabled/POSCTL、loss events | `true`；当前 armed POSCTL，mode loss `0` |
| sustained refresh | elapsed/coverage/max gap、accepted/rejected | `8.172 s` / `8.062 s` / `0.485 s`；68 / 0，不靠过期 target 维持 |
| heartbeat continuity | timeout、首尾最大 gap、stale/failsafe | `3.0 s` / `2.844 s`；continuity gate `true` |
| response | `flight_response_gate_passed`、position source/sample/displacement | `true`；171 个 PX4 `GLOBAL_POSITION_INT`；最大 WGS-84 水平位移 `0.818282 m >= 0.5 m` |
| provenance | `gazebo_truth_consumed`、`gazebo_pose_injection_used` | 均为 `false`；位移不是 truth/teleport 制造 |
| resource bound | frames/polls/limit/exit | 2,665 / 2,704 / 100,000；limit 未触发；exit=`accepted` |
| cleanup | DISARM、neutral、stop/rm、残留检查 | 完成；容器无残留 |

canonical JSON 为 661,936 bytes / SHA-256
`B56F20AB40B3E8AF9DF169FAAED9D4F326FFCDFDD052ED0FC6FF804FD7C44073`；container log 为 2,200,000 bytes /
SHA-256 `04F57BBF9F7A9FEB7EF5C6990DBAF4187D2AE8B5E3C49E922E3919B568B10EBD`。telemetry、AI 和顶层
flight elapsed 都冻结为 `8.172 s`。v1/v3 失败来源继续保留，v2 只作为首次成功诊断；当前相关回归
`105 passed`，Ruff/compileall 通过。当前代码全量回归为 2,907 tests：`2906 passed, 1 skipped,
0 failed, 0 errors`；JUnit 411,702 bytes / SHA-256
`A2C2595C49D0B663468D09274D22976CC1B4D9128425BB23458445B1C5397F51`，唯一跳过为 Windows symlink
权限项。

该门只覆盖 x500 的 PX4-reported position response，不覆盖 Gazebo camera detection → ranging →
target geolocation → QGC Fly View，也不覆盖 actuator telemetry、airborne flight、fixed-wing/VTOL 动态对等、
真实 Jetson/相机/Pixhawk、室外 GNSS、HIL 或 REAL。v55 未改变 QGC/产品 parity，未生成新 EXE，未加入
E7 三机型 external 六源。最近 E7 strict 仍是 v54 的 exit `2` / 32 blockers，不能据此减少发布阻断。

## v54 Gazebo wire-v2 与非平地动态 runtime 门（2026-09-05，`v0.3.0-pre.8`）

三个 Gazebo 机型现在通过官方 3-D OdometryPublisher 输出 world pose 和 body velocity；
`MDGZSIM2` wire v2 将其与 image、CameraInfo、NavSat 在有界 skew 内组合，并由 backend 转换
ENU/FLU → NED/FRD。AGL 只允许来自 qualified NavSat MSL − scenario DEM MSL；Odometry local-up
绝不作为 AGL，所有输入缺失/错配/越界分支 fail-closed。

| 证据 | 值 | 当前结论 |
|---|---|---|
| VTOL wire-v2 runtime v3 | 27,570 bytes / `224F79095247521ED6AE320299CD1E428C4E49072375C8BB2647950E464A8525` | 外部容器、telemetry 与 3/3 image/NavSat/Odometry 帧通过 |
| 非平地 validation deterministic v1 | 4,215 bytes / `FB1F532E340F7E468DE0002824AC374C12C5960391F1EC3B1A6BBE6871CAD2D2` | 8 步共享 AI/authority/watchdog 通过；repeat hash 一致；truth 未加载 |
| 非平地 x500 external runtime v2 | 27,089 bytes / `8FB0FE4338EEA71021C7CCD8515ACFB2577847A2517297126C921AE4D825C4B0` | SDF/DEM/manifest hash 绑定，3/3 wire-v2 帧；AGL `0.2303031043 m`；local-up-as-AGL=false |
| v54 三机型 3-D control binding | 2,359 bytes / `78066C298A9B9EEDB012B7596D9B7DA66E334DE7449EACDF1CCD59513181B00C` | 当前 Odometry SDF 字节的 fixed-wing / multirotor / VTOL deterministic preflight 通过；external runtime/视觉/硬件不宣称 |
| v54 Docker 安全边界 | stop/rm exit `0`；无残留；`flight_control_commands_sent=0` | v54 sensor runtime 只发 GCS heartbeat，没有 ARM、mission、actuator 或 transition 命令 |
| 当前聚焦回归 | `265 passed`；三维/E7 专项 `22 passed`；Ruff/compileall pass | backend、Docker、scenario、CLI、文档、parity 与证据策略通过 |
| v54 完整 Python 回归 | `2888 passed, 1 skipped, 0 failed, 0 errors` / 2,889 tests | JUnit 409,003 bytes / `C3D01A9F3A157A3B57225319A67FA7EE79883AAB70C025DA14E896F0C1EA4532`；仅 Windows symlink 权限项跳过 |
| 当前 E7 strict | exit `2` / 32 blockers | v54 3-D source 已绑定；manifest/report/strict SHA-256 为 `913CB8FE...` / `72127330...` / `E20D2523...`；发布仍关闭 |
| 当前 parity | 5,098 bytes / `C28AC3C300DDACAD7B97097EC80987AED78E8EB8B19101AD98FD8D66F7985739` | 五组共享产品契约重算后未变；QGC 源码未改 |
| 人工 QGC 视觉 / 坡地航迹 / 硬件 / 室外 GNSS survey / HIL/REAL | `not_claimed` | 继续阻断最终发布 |

v54 这一门把 v53 的非平地 SDF/DEM 从静态资产推进到真实 headless PX4/Gazebo sensor runtime，但该次车辆只在
home 原点采样，尚未沿坡面航迹飞行，也没有做目标检测/测距/落图闭环。因此不能把它升级成完整 SIM、
硬件、室外精度或最终发布证据。版本保持 `v0.3.0-pre.8`。

三个 model SDF 新增 OdometryPublisher 后，v52 三机型证据的 model hash 已按设计失效；当前发布门没有
复用或放宽该旧证据，而是重新生成 v54 三源与 binding。E7 默认 wrapper 继续保留 v52 的 parity-valid
external 六源，但 3-D control 输入已固定为 v54。当前 manifest/report/strict 的完整 SHA-256 分别为
`913CB8FE13245A25FA5BEA73F19ACF3294CE90A42C6EE0CF419E8EE9D34349D7`、
`7212733034F21994D20D1A41F2B4CF789A213B67981DA379A63D731124CB1900`、
`E20D2523E771D203698D8EB35462D83279CA2B2E350A9BA936361A2C150D8B84`。

## v53 非平坦 SDF/DEM 资产真值门（2026-09-05，`v0.3.0-pre.8`）

新增独立非平坦 world/DEM 对。SDF 使用 ENU 坐标和 `atan(0.1)` roll，解析真值为北向
`+0.1 m/m`、东向 `0 m/m`；验收器从 SDF transform 和 collision plane 独立建立解析平面，不复用 DEM
插值或 ray-cell root 公式。它比较 25 个节点、16 个单元中心和 7 条确定性跨单元射线，并重新核对当前
五组 release parity。

| 证据 | 值 | 当前结论 |
|---|---|---|
| v53 asset-truth v2 JSON | 11,952 bytes / `CCB34D5AE3088DE1A66CCF4E6D521C7E009DEF1CD25051E6DDA61DC4A526FB9B` | schema 通过，deterministic asset truth 通过 |
| 表面采样 | 25 nodes + 16 cell centers | 最大高程 `3.553e-15 m`、坡度 `1.388e-17 m/m` |
| 射线真值 | 7/7；6 条跨单元 + 1 条垂直基准 | 最大斜距与北向误差均 `5.684e-14 m`，门限 `1e-7 m` |
| mutation / 输入安全 | DEM 高程、SDF slope、origin、DTD/entity、外部 URI | 全部 fail-closed；聚焦回归 `107 passed` |
| v53 完整 Python 回归 | `2865 passed, 1 skipped, 0 failed, 0 errors` / 2,866 tests | JUnit 404,145 bytes / `2E54AD65923CDB93D1BD570A0C726FCBFD32316FB3A0EB5A8D3B13A116205092`；仅 Windows symlink 权限项跳过 |
| 当前 parity | 5,098 bytes / `C28AC3C300DDACAD7B97097EC80987AED78E8EB8B19101AD98FD8D66F7985739` | 与共享源码逐字节一致，v53 未改产品契约 |
| 外部 Gazebo/PX4 | `external_gazebo_runtime_gate_passed=false` | 未启动，不能作为动态 SIM 证据 |
| QGC 视觉 / 硬件 / 室外 GNSS / HIL/REAL | `not_claimed` | 继续阻断最终发布 |

因此，本门只关闭非平坦场景文件与共享 DEM 算法之间的确定性几何真值子门。下一步仍需外部
Gazebo/PX4 camera/navsat 运行、人工 QGC Fly View 验收，以及独立的室外 survey、HIL、REAL。版本保持
`v0.3.0-pre.8`，最终 `v0.3.0` 未发布。

## v52 DEM/GNSS 坡地测距门（2026-09-05，`v0.3.0-pre.8`）

共享测距核现在可从合格的 WGS-84/MSL GPS 样本和严格局部 DEM 求真实地形交点，并把 GPS、DEM、坡度、
杆臂和三轴射线误差传播到距离区间。DEM 配置存在时，时间偏斜、越界、向上射线、无交点或无界误差均
fail-closed；不会用平面 camera-ground 结果冒充地形结果。raw replay、GNSS sidecar 与 live 使用同一绝对
MSL 高度字段，因此四种 runtime mode 的核心契约一致。

| 证据 | 值 | 当前结论 |
|---|---|---|
| `configs/release-parity.json` | 5,098 bytes / `C28AC3C300DDACAD7B97097EC80987AED78E8EB8B19101AD98FD8D66F7985739` | 24 项 parity 专项通过 |
| DEM/ranging/live/replay/protocol/stack 聚焦回归 | `458 passed` | 共享地形、telemetry 与 fail-closed 链通过 |
| v52 完整 Python 回归 | `2855 passed, 1 skipped` / JUnit `E82F4F8FCF3C0639B272ECCECC06B8BFE85B8D946B78C3E8B29A1F1BFEDCE077` | 2,856 tests，0 failed / 0 errors |
| v52 三机型 binding | 2,368 bytes / `2B886AEF69197268A8658E1A5C8AA24D753DE4E560934CC245A667D7AA49948B` | deterministic protocol/asset preflight 通过 |
| v52 external 六源 join / revalidation | `3B82910E3122DF3A929DA24E5D4C119916B0BE22035BBEB3C25208CE491F7A79` / `CE6A3E4DE25D3E60466BA20FFF70C9FC51B59A7455AC5506935E2A3C3AD460B6` | 既有 software-only 源文件按当前 parity 重算通过；不是新硬件运行 |
| v52 E7 strict | exit `2` / 32 blockers | manifest/report/strict 均重新绑定，发布仍关闭 |
| 三机型 external runtime | `external_runtime_gate_passed=false` | 本轮没有启动外部 PX4/Gazebo 进程 |
| 人工视觉 / 硬件 | `not_claimed` / `not_claimed` | 仍是独立必需门 |

v52 当时的三个机型 DEM 与平坦 Gazebo world 一致，只验证代码和输入契约；后续 v53 的独立非平坦
资产真值也不等于外部 3-D 视觉、室外 GNSS/距离精度、真实 Pixhawk/Jetson、HIL/REAL 或实飞。版本
继续保持 `v0.3.0-pre.8`。

当前 strict 记录位于
`artifacts/evaluation/terminal-continuation-v52/release-gate-pre.8-current-20260904T201121Z-v6-bound*.json`；
manifest/report/strict SHA-256 为
`897EFDFE8EA0F3A6DD50136D957804548870FE9A1A2E69AD93C7959DAA7B999D`、
`4F968E128B72CFFC89BE4E2214A947D01696DBED970F90B2C7704B6A526E244D`、
`87DCEC746D2D2B6B33369A11405F5AF1398770B50A90799DE9F0A33A666DA899`。

## v51 RANGE_STATUS bit 31 与 SIM 地理资产门（2026-09-05，`v0.3.0-pre.8`）

三轴不确定性失败现在能完整穿过认证 wire：三种内部细粒度 reason 在发送边界规范化为
`target_geometry_uncertainty_unbounded`，占用 reason mask bit 31；Python/QGC C++ 均认可完整
`0xFFFFFFFF` mask。该 bit 只表达“几何误差无界/跨地平线”的 fail-closed 状态，不允许携带有限距离。

SIM 侧新增真实地图/DEM 资源门。三个 runtime manifest 指向仓库内 GeoJSON 和 `.dem.json`，resolver
核对 scenario identity、WGS-84、world origin、数据边界、containment、非符号链接及 SHA-256；资产缺失
或内容错配时 stack 和 VTOL acceptance 均停止，不再把非空 URI 算作 available。

| 证据 | 值 | 当前结论 |
|---|---|---|
| `configs/release-parity.json` | 4,860 bytes / `FA7EF5D6B1E8042F88F119D0FB9832540A1517B2320784956BD990567F77DD43` | 源码重新计算，24 项专项通过 |
| 测距 wire/status 专项 | `97 passed` | Python bit 31、别名去重和 stale 门通过 |
| stack/scenario/VTOL 专项 | `45 passed` | 三场景地图/DEM 文件与身份绑定通过 |
| QGC operator protocol self-test | exit `0` | C++ bit 31 解码通过 |
| Release / canonical portable EXE | 46,576,128 bytes / `06E7B1BBEC1B96DC6E5A231B12B88725122476075ED86A24BBEF554CB899E5FE` | 字节一致；仍须完整 portable 运行时 |

当前 DEM 为平面网格，本表不构成坡地 3-D 动态验收；也不构成真实 Jetson/Pixhawk、室外 GNSS/距离
真值、人工 Fly View、HIL/REAL 或最终 release 证据。版本保持 `v0.3.0-pre.8`。

## v50 三轴安装角不确定性门（2026-09-05，`v0.3.0-pre.8`）

相机安装调查的 `boresight_sigma_deg` 现在是 pitch/yaw/roll 三个独立安装角各自的共同 `1σ`。三个轴
分别生成正负 NED 射线端点；同一组端点同时用于 camera-ground slant range、direct ground range 和
absolute bearing 传播。任何端点越过地平线都会 fail-closed。该算法由 REPLAY/SIM/HIL/REAL 共用，
不会因 runtime mode 切换，也不创建新的 UI 或飞控路径。

| 证据 | bytes | SHA-256 | 结论 |
|---|---:|---|---|
| `configs/release-parity.json` | 4,678 | `0DA306BCD0A635DF70584BB14514153FB05433A452F52F7D58FB32F7B1C46336` | estimator + camera schema 已绑定且可逐字节复现 |
| `terminal-continuation-v50/three-axis-boresight-geolocation-acceptance-v50-schema-bound.json` | 5,594 | `9400FF54A6347A9F7D0FCE116CAA497E01438EFEC9D4DF39E824BD3C408536DD` | 软件几何、schema parity、回归与边界汇总通过 |
| `terminal-continuation-v50/simulator-external-runtime-ai-join-20260905-v50-schema-bound-pre8.json` | 5,491 | `7ACCEDDF5B218BEACFB38C42B2814605FD3127420BFA90837E968BE906676A86` | software-only join 通过 |
| `terminal-continuation-v50/simulator-external-runtime-ai-revalidation-20260905-v50-schema-bound-pre8.json` | 570 | `E7CC9B6A5C28A0D7E5671516933DE232145BC254D0B7BF8C0249C5B9F802AAEB` | 独立重算通过 |
| `terminal-continuation-v50/full-regression-v50-three-axis-boresight-schema-bound-20260905.xml` | 398,799 | `862C80D0D1AAF52AA8F9ABEE2FF4E8691C6A771FD45EE5F54A2B9E49ED08577E` | 2,828 pass / 1 skip / 0 fail |

`core_algorithm_hash` 为 `f5ae7dca085db1556ad2c63ad6b08c1f155b229eb72ad78fb6bc6653e7b46044`；UI 与 PX4
Mission path 没有改变，因此本轮没有重复链接 QGC，portable 继续使用 v49 已验证的 SHA-256
`EE2A6A5E7B4561D56D6130FE9D81AEAC8F7093EB1A352792CB85AB353133F57D`。

v50 schema-bound strict run 的 manifest/report/strict-report 为 20,990 / 54,829 / 54,829 bytes，SHA-256：

```text
BD8E6D8C7A0DBF7817232BB8957BFDEC6C401D9FE685D4CCC11F4B0EDD9F09A3
408EEB74BD0340F98010A91514B352CB36AB32943F6987624A251D611EB60DEB
73321CCD3C49769ADEA0774BCA45C45CC0ABFE873EF377DEC8BB4F217B99E35E
```

strict evaluator 按预期退出 `2` 并保留 32 项阻断。软件探针只能证明不确定性传播和 fail-closed 行为，
不能证明安装角 survey 数值正确、室外定位精度提高、真实 GNSS/相机/Pixhawk 已接通、人工 Fly View、
HIL/REAL 或最终 `v0.3.0` 已通过。

## v49 模式 3 输出锁定 UI 真值门（2026-09-05，`v0.3.0-pre.8`）

v48 已让 QGC 接收实际 provider 的动态 `flight_control_enabled`，v49 补齐显示层最后一条语义缝隙：
只有 approach 报告 active **且**当前飞控输出权限为 true，`fixedWingAimActive` 才为 true。目标已确认但
provider 未重新认证或已撤销权限时，原生 Fly View 横幅明确显示“飞控输出锁定”，并声明只显示姿态建议、
不会发送姿态或模式命令。stale/abort/cancel/waiting 分支均先于 active 文案判定。

| 证据 | bytes | SHA-256 | 结论 |
|---|---:|---|---|
| `configs/release-parity.json` | 4,622 | `B8D2918DBC3040596A86BB6B9FC8FB7843E660929DBCD2D677C70D3592F69167` | UI contract 已重算 |
| `terminal-continuation-v49/qgc-native-selftests-v49.json` | 2,016 | `FFE34FF1B6CF7D3D933C56DB7F2BCFCE0C2D789A3B487F8E5D045C254A839667` | 3/3 原生协议自测通过 |
| `terminal-continuation-v49/qgc-mode3-output-lock-acceptance-v49.json` | 4,935 | `91D8B4800E21A7523F9E909C51C4E113D10D469310A97D9E85EDB70D59FDAE30` | 构建、offscreen boot、测试与边界汇总通过 |
| `terminal-continuation-v49/simulator-external-runtime-ai-join-20260905-v49-pre8.json` | 5,491 | `217BBC60F506D82E5159164C7A17D06345843B1A7BFD539FCAD42F4344FACC28` | software-only join 通过 |
| `terminal-continuation-v49/simulator-external-runtime-ai-revalidation-20260905-v49-pre8.json` | 557 | `302969731360683BEDD629BF54CE7ED365D39BC9BC45D6B8296EEB12E9280C29` | 独立重算通过 |
| `terminal-continuation-v49/full-regression-v49-mode3-locked-banner-20260905.xml` | 398,541 | `79083F9262E924559B84C117D494D84533CAFFD47857648C19DB869250C39764` | 2,826 pass / 1 skip / 0 fail |

Release 与 portable EXE 字节一致，均为 46,575,616 bytes、SHA-256
`EE2A6A5E7B4561D56D6130FE9D81AEAC8F7093EB1A352792CB85AB353133F57D`；隐藏 offscreen simple boot
退出 `0`。`ui_contract_hash` 为
`1dc7e2ae6edbb7ed2f4b212232ebf24fcb2fd54cf84373e4e2941af0dc88c992`，PX4 Mission path hash 未变。

v49 strict run 的 manifest/report/strict-report 为 20,964 / 54,790 / 54,788 bytes，SHA-256：

```text
913129A67AB4E7E9FA5A90C2E02BC4B67F91983C77AC468DA87965065F82A469
B9758009DBAFA7048DC7756D165FFC90F737A32F09742F1E3DDB120D3ACFD501
10E809DD5C745E154FF7100F0D45E7F8BFAA501494D65668D4AB461F33A42D55
```

strict evaluator 按预期退出 `2` 并保留 32 项阻断：external 子门通过，但
`px4_mission_feedback.present=false`，人工视觉、现场硬件、HIL/REAL、室外 GNSS/距离真值和最终 release
通道均未关闭。offscreen boot 和静态 QML 测试不等于 operator visual acceptance。

## v48 provider 独立实时再认证门（2026-09-05，`v0.3.0-pre.8`）

v47 的 signed profile、车辆、固件、端点和 signing identity 是**静态授权条件**，不是新连接已经实时
认证的证明。v48 将 launcher probe 与实际 flight provider 明确分成两个 freshness 域：

```text
receive-only launcher probe
  -> signed profile + observed identity 静态校验
  -> probe closed
  -> new ProfileBoundFlightControlProvider starts LOCKED
       -> own fresh HEARTBEAT
       -> own same-source AUTOPILOT_VERSION
       -> exact vehicle / firmware / airframe / endpoint / signing match
       -> live_binding_attested=true
       -> REAL flight profile + explicit opt-in 才可能 enabled
```

输出操作不能触发第一次 attestation。provider 尚未 attested 时，snapshot 可以接收，但不能发送 ranging
或 RC stream request；姿态 setpoint、模式命令和其他飞控输出同样被阻断。heartbeat gap 超过 freshness
窗口、heartbeat 稳定身份变化、固件不匹配或 `close()` 都会原子撤销 attestation、清除缓存
`AUTOPILOT_VERSION` 并恢复锁定；相同 system/component 重连也必须出现新的版本帧。HIL 无论 attestation
状态如何仍保持静态 `output_locked=true`。

CLI/QGC 消费侧不再复制启动时的权限快照。`FixedWingAimExecutor.flight_control_enabled` 动态读取
transport，`LiveApproachHilCoordinator` 在每个状态包重新调用权限 supplier，supplier 异常或非布尔值
都 fail-closed。`live_camera_started` 将 `fixed_wing_aim_control_configured`、静态
`flight_control_authorized`、实时 `live_binding_attested`、当前 `flight_control_enabled` 与
`output_locked` 分开输出。

v48 证据：

| 证据 | bytes | SHA-256 | 结论 |
|---|---:|---|---|
| `configs/release-parity.json` | 4,622 | `49D9B243A26591784DA0651A0B8789C5AEC0E162911199EC447C38EE370152D6` | pre.8；PX4 path 加入 approach/aim control |
| `terminal-continuation-v48/simulator-external-runtime-ai-join-20260905-v48-pre8.json` | 5,491 | `65CCC80DC09CA1094884D82D24CC499BAD12AA382986572E75EA69CC2BDAECC6` | software-only join 通过 |
| `terminal-continuation-v48/simulator-external-runtime-ai-revalidation-20260905-v48-pre8-v2.json` | 557 | `E26B5149CBECB877529B85A0DD51B32864670110412EA62D0B3EADD056604B2F` | 独立重算通过 |
| `terminal-continuation-v48/full-regression-v48-live-provider-attestation-20260905.xml` | 398,376 | `59D78D2D621C4EE6395E4EE341D3101E3CC7424531B4C2950C3D0165D9749267` | 2,825 pass / 1 skip / 0 fail |

`px4_mission_path_hash` 当前为
`544079908cbb34f9b6a28fe02e344153595cb1b983271aaa027c50d1e2247ce1`。首次重验把 expected release
误写为 `0.0-pre.8`，因此
`simulator-external-runtime-ai-revalidation-20260905-v48-pre8.json` 保持
`source_recomputed=false` / `gate_passed=false`，只作为失败诊断保留；wrapper 与 E7 canonical 路径只
引用 `-v2.json`。

v48 strict run 的 manifest/report/strict-report 为 20,967 / 54,793 / 54,793 bytes，SHA-256：

```text
1CB8D0EC80DA10DFA09265F5984B3D98A0C916120FD45A6434911F084CF91F66
298F1FF17DC178FE7B09149CCBEE1ECE55B3834B094F9625FF777E0FA91BAC7B
0A25D5D564A5A5363D2C317BFE89CCF4A5ED47ED1613D245209EB8120DE1D912
```

strict evaluator 按预期返回 `2`，列出 32 项阻断；报告同时确认
`simulator_external_evidence.present=true/passed=true` 与 `px4_mission_feedback.present=false`，总门仍为
`gate_passed=false` / `release_allowed=false`。因此 v48 是软件权限时序收口，不是 V6X、HIL/REAL、
室外 GNSS/距离真值、人工 Fly View 或最终 `v0.3.0` 的通过证据。

## v47 firmwareless REAL output 旁路收口（2026-09-05，`v0.3.0-pre.8`）

v47 补齐 v46 之后仍存在的一条格式兼容缝隙：legacy REAL `flight` profile 没有
`firmware_identity_hash` 时，可以继续由泛用 loader 做无输出诊断，但不能因调用方传入
`allow_flight_control_output=true` 而取得输出权限。REAL 输出判定现在无条件要求：

```text
signed REAL flight profile
  + explicit output opt-in
  + profile firmware_identity_hash present
  + live observed firmware_identity_hash present
  + profile hash == observed hash
  -> only then flight_control_enabled=true
```

任一固件条件失败都会同步置 `identity_valid=false`、`profile_valid=false`、
`flight_control_enabled=false` 与 `output_locked=true`。实际 HIL/REAL launcher 还总是传入
`require_firmware_identity_binding=true`；observer profile 始终只读，底层
`ProfileBoundFlightControlProvider` 构造器也拒绝 firmwareless flight profile，因此 loader、launcher 与
每次输出前的动态 rebind 三层使用同一 fail-closed 结论。

当前 parity 为 4,529 bytes、SHA-256
`B40053E5EE74D09FA890E506BEACF7392288778AAE22F449016FC88D1563BBCF`，
`px4_mission_path_hash=63674e1adedc5884561ffa62f6c6b4f35d1e4a067bb5114e29ab3a6d305917c5`。v47
external join/revalidation SHA-256 为
`456524B74F70F8431C186557255E067C6F70DFB4A8E6C42D93F2C16622DA1890` /
`F4000DF499E8DEE33C4D795D49B6CE177347DEA5E661959118277E450692907C`，两份都通过，但只重验既有
software-only Docker/SITL 来源。

完整回归为 `2820 passed, 1 skipped`（2,821 tests），失败/错误为 0；JUnit 397,695 bytes、SHA-256
`557FA4D8D306193388DB3D5600A8E5AD4228C2C2E494112F4857EB201EDD8934`。report-only run 的
manifest/report 分别为 20,964 / 54,790 bytes，SHA-256：

```text
3874360C06854434BBB6AC312D4D8610281F7F44211401E03F40DFDCA1DC8798
1EEA0AAEC89736DC4441DAEDD9D4D2C0C672CCA5B7F4362FAF6E08585641F709
```

report-only 返回 `0` 且 `gate_passed=false` / `release_allowed=false`。strict run 返回 `2`，其
manifest/report/strict-report 均为 20,964 / 54,790 / 54,790 bytes，SHA-256：

```text
88C4E86F007F92F9ABC5BB9D8D507BEF3E4933B9A1374F059025EE2B351C92C6
2E871C79E3F58C7520B0EF70993AF7C2B1BFE29FEB8E2BCDD75CBB58B9F72155
CC62CD5D4BBFCB026022DEDE23483746AF7E9C76C32AFF31BAD16603FF5CC931
```

报告明确记录 `simulator_external_evidence.passed=true` 和
`px4_mission_feedback.present=false`。replay-set deployment 精度/评审、陈旧且未就绪的现场链路、各模式
人工 Mission/Fly View、HIL/REAL 硬件/控制、室外 GNSS 与 prerelease 通道仍是阻断项，最终
`v0.3.0` 没有发布。

## v46 firmware-bound RuntimeProfile/E7 门（2026-09-05，`v0.3.0-pre.8`）

v46 把 v45 的隐私保护固件身份提升为 HIL/REAL Mission-feedback 发布输入，同时保留旧 profile 的历史
可读性。新的现场 `provision` 必须在自己的 probe 窗口内同时观察到：

```text
fresh PX4 HEARTBEAT from system/component
  + complete AUTOPILOT_VERSION from the exact same source
  + observed_at inside [probe_started_at, probe_completed_at]
  + resolved endpoint and matching Mission airframe
  + read_only=true and messages_transmitted=0
  -> signed RuntimeProfile with firmware_identity_hash
```

泛用 loader 可以打开未含 `firmware_identity_hash` 的 legacy HIL/REAL profile；这只保证历史诊断可读。
HIL/REAL feedback observer 与 E7 六叶 binding 会额外强制 profile/observed/report 三侧固件 hash 都存在且
一致，并以 `all_firmware_identities_bound=true` 作为通过条件。缺失 profile hash、缺失实时版本身份或
hash 不一致都单独 fail-closed。原始 `uid2`/`uid` 仍不进入 profile、report 或 release manifest。

当前 parity SHA-256 为 `3A85B02436CD545E1409B842D28AC3A600DC02749B5F4A5D2130A525D4A21F95`，
`px4_mission_path_hash=3639c0a56d8d9ab3e799242ff190838a8f9959d07a938996b36e5e86ab775e5a`。
v46 external join/revalidation SHA-256 为
`31079BDC34DA919613C130B5466C13C0003D4401DD7B2CC030DE4847A15E23F0` /
`EBAB5126102B511A31614273C83F1DDF6D149D15344A8108A20AFE3C807F00B5`，只重验既有
software-only Docker/SITL 来源。真实六份 profile/report 尚未采集，因此 E7、HIL、REAL、硬件、室外
GNSS、人工视觉和最终 `v0.3.0` 均保持关闭。

完整回归为 `2816 passed, 1 skipped`（2,817 tests），JUnit 397,164 bytes、SHA-256
`18EDEF1953C0F4E4EA452AF210185B5074C4D61C3E02F331B368E802806401F1`。report-only run 返回 `0`，
且 `gate_passed=false` / `release_allowed=false`；strict run 返回 `2`。strict run 生成的
manifest/report/strict-report 分别为 20,950 / 54,745 / 54,743 bytes，SHA-256：

```text
CC1BB8BF9F68AD904CA90049293D7C5526005217F199EE9C3ECB369AF5778C33
CA5601CB7D3BB18CE0387E9AD8809ED3087EC55A0751544EBD6303738069AC00
F413957547B5AE31A3F5E48AA1B68BC5D1A8EA155EE4E1C51FE7B9CA54831020
```

该报告明确记录 `simulator_external_evidence.passed=true` 与
`px4_mission_feedback.present=false`。其余阻断仍包括 replay-set deployment 精度/评审、陈旧且未就绪的
真实链路、各模式人工 Mission/Fly View、HIL/REAL 硬件/控制，以及 prerelease 版本通道。

## v45 PX4 firmware identity 前置门（2026-09-04，`v0.3.0-pre.8`）

HIL/REAL 要求“与真实 V6X 固件一致”时，普通 heartbeat 的 system/component/MAV_TYPE 不足以区分同型
飞控。v45 先增加零发送的被动 `AUTOPILOT_VERSION` 身份层：严格绑定 heartbeat source，规范化软件/
board/vendor/product/capabilities/custom-version 字段，对 `uid2`/`uid` 只保存 domain-separated SHA-256，
并给完整身份再计算 canonical SHA-256。

```powershell
python -m multidetect pixhawk-check `
  --endpoint 'udp:0.0.0.0:14550' `
  --samples 50 --interval-seconds 0.1 `
  --require-fresh-link --require-autopilot-version
```

这个命令不主动请求版本帧，`messages_transmitted` 必须为 `0`。无版本帧、错误来源、畸形 wire 字段、
未来观察时间或发送计数非零都会使门失败。v45 截止时 release manifest 尚不消费该字段；v46 已完成
RuntimeProfile 与 HIL/REAL 六叶 binding 的软件侧强制绑定，但真实 V6X 采集仍未完成。因此本节没有关闭
任何硬件、HIL、REAL、人工视觉或最终 v0.3.0 门。

v45 parity SHA-256 为 `02BF3559EDE0C955A700B743436EA9AEA652DA29F1FCCCEC8E4C7D3A1F642DF6`，
`px4_mission_path_hash=9dfcd64df28141ad9f08f439bf839b7a49e2b0ebdefb9cd8367d9a720e2a91f8`。
当前 manifest/report/strict 分别为 20,950 / 54,745 / 54,745 bytes，SHA-256：

```text
1FD698799B24F6C8F4CCB66D9431CEBE11E647544CFBE3DCF75E43D4872EE5EE
FCD4FCE341586CD03419DACE3B47245886A948A93F60385A503C5A5D910EE53C
405566B94F95B9041888CEFB811D428CB97D2305DB26D4E94DD3D92E40BC7D8A
```

report-only 返回 `0` 但 `gate_passed=false` / `release_allowed=false`；strict 返回 `2`。完整回归为
`2808 passed, 1 skipped`，未把缺失的现场证据写成成功。

## v44 现场 profile provision 门（2026-09-04，`v0.3.0-pre.8`）

v43 observation v2 只能接受已经绑定实际车辆、端点和 Mission airframe 的签名 RuntimeProfile。
v44 为这个 bootstrap 步骤增加只读 provision 命令，避免操作员手工计算三个 identity hash：

```powershell
$env:PYTHONPATH = 'src'
$env:MULTIDETECT_RUNTIME_PROFILE_HMAC_KEY = '<secure 32-byte-or-longer key>'

python -m multidetect.px4_mission_feedback_cli provision `
  --mission .\missions\vtol.json `
  --endpoint udpin:0.0.0.0:14550 `
  --runtime-mode hil `
  --runtime-profile-hmac-key-env MULTIDETECT_RUNTIME_PROFILE_HMAC_KEY `
  --runtime-profile-key-id field-runtime-profile-2026-09 `
  --expected-system-id 1 `
  --expected-component-id 1 `
  --out .\profiles\hil-vtol.json
```

也可使用 `multidetect-px4-runtime-profile-provision` 入口。命令从 Mission 的 adapter ID/hash 自动确定
fixed-wing、multirotor 或 VTOL，再核对 heartbeat MAV_TYPE。只有 resolved endpoint、fresh PX4
heartbeat、完整 system/component/autopilot/vehicle-type/MAVLink-version、`STANDBY/ACTIVE`、只读 provider
和零发送全部成立时才签发。HIL 拒绝 signing identity；REAL 必须通过
`--runtime-signing-identity-hash` 或 `--runtime-signing-key-hex-env` 提供 signing identity。两个原始 key
都不写入 profile 或控制台摘要。

写入过程先生成同目录临时文件，再用同一 observed identity、HMAC 和 key ID 调用生产 profile loader；
只有 `profile_valid=true`、`output_locked=true` 且 `flight_control_enabled=false` 才原子替换。默认禁止覆盖，
`--overwrite` 必须显式给出。该步骤生成 capability input，不是 Mission feedback、硬件、HIL 或 REAL
通过证据；每份 profile 随后仍必须在独立 v2 observation 中与新的 heartbeat/endpoint 重新匹配。

当前 v44 parity SHA-256 为
`BB5D6285E81B813494BF56432F76FB045F7F4DAC454553F43B4041728F8C2F62`，PX4 path hash 为
`a8d5a95d18e7efb5c829f53a8402c0d8bc3019d6012186351196a20c553b1f87`。software-only external
join/revalidation SHA-256 为
`3F01945CAC2E79295279418154D6DFF3F490DEF09FFE0F3AB71A2580FA2F28E8` /
`D6240C13461B2633DE80D431CD5877AF6694703F71EDFD4E28FC7AA6BAA8B4EE`。当前 E7
manifest/report/strict-report 为 21,036 / 54,830 / 54,830 bytes，SHA-256 分别为
`432A9835B2D061B15E8CB9D54759FBF36D9D1E511F0FD09E26E14953DB0DACDF`、
`423D15645792457F7107BF7E1C9A59F0D1ED125F87733A72B62B80DF26CB4527`、
`2D5510308BC3CB49C517F69C86EEE09F2941274A41193489970520F6972D72BC`；strict 返回 `2`，
`px4_mission_feedback.present=false`，真实六源及总门继续关闭。

聚焦回归为 `47 passed`，CLI help smoke 退出 `0`；完整 Python 回归为
`2793 passed, 1 skipped`、退出码 `0`。3,226-byte 最终日志位于
`artifacts/evaluation/terminal-continuation-v44/full-regression-v44-runtime-profile-provision-final-20260904.log`，
SHA-256 为 `C437BD893FF13E5E161161F6A762A821B661CC1C44A7534FF5D27573A4938C5F`。

## v43 签名 RuntimeProfile 与反馈会话身份绑定（2026-09-04，`v0.3.0-pre.8`）

E7 的 `px4_mission_feedback` 记录现在使用 binding schema v2 和 observation schema v2。每个
HIL/REAL × fixed-wing/multirotor/VTOL 叶节点必须直接提供：

```text
MODE VEHICLE EXECUTABLE_MISSION SIGNED_RUNTIME_PROFILE FEEDBACK_REPORT_V2
```

观测器先用 profile HMAC key 验证签名文档，再将完整 heartbeat 的 system/component、PX4 autopilot
ID、vehicle type、MAVLink version 派生成车辆 identity；configured/resolved endpoint 与 baud 独立派生
endpoint identity；Mission 提供 airframe profile identity。三类 identity 必须与 profile 完全一致。
HIL 必须是 `hil` profile 且不能携带 REAL signing identity；REAL 必须是带 signing identity 的
`flight` profile。该子门在 REAL 下同样显式关闭 output opt-in，因此六项都必须报告
`flight_control_enabled=false`、`output_locked=true`。它观测执行反馈，但不创建任何发送链路。

采集示例：

```powershell
$env:PYTHONPATH = 'src'
$env:MULTIDETECT_RUNTIME_PROFILE_HMAC_KEY = '<secure 32-byte-or-longer key>'
python -m multidetect.px4_mission_feedback_cli `
  --mission .\missions\fixed-wing.json `
  --endpoint udpin:0.0.0.0:14550 `
  --runtime-mode hil `
  --runtime-profile .\profiles\hil-fixed-wing.json `
  --runtime-profile-hmac-key-env MULTIDETECT_RUNTIME_PROFILE_HMAC_KEY `
  --runtime-profile-key-id field-runtime-profile-2026-09 `
  --duration-seconds 30 `
  --out .\feedback\hil-fixed-wing-v2.json

python -m multidetect.px4_mission_feedback_cli verify `
  .\feedback\hil-fixed-wing-v2.json `
  --mission .\missions\fixed-wing.json `
  --runtime-profile .\profiles\hil-fixed-wing.json
```

REAL 采集另需 `--runtime-signing-identity-hash` 或 `--runtime-signing-key-hex-env`。原始 HMAC/signing key
不会进入命令输出或报告。旧 v1 报告仍可做历史诊断，但不能携带 runtime binding，也会被 E7 拒绝。

清单输入示例：

```powershell
python scripts\create_release_gate_manifest.py `
  --px4-mission-feedback-evidence hil fixed-wing .\missions\fixed-wing.json .\profiles\hil-fixed-wing.json .\feedback\hil-fixed-wing-v2.json `
  --px4-mission-feedback-evidence hil multirotor .\missions\multirotor.json .\profiles\hil-multirotor.json .\feedback\hil-multirotor-v2.json `
  --px4-mission-feedback-evidence hil vtol .\missions\vtol.json .\profiles\hil-vtol.json .\feedback\hil-vtol-v2.json `
  --px4-mission-feedback-evidence real fixed-wing .\missions\fixed-wing.json .\profiles\real-fixed-wing.json .\feedback\real-fixed-wing-v2.json `
  --px4-mission-feedback-evidence real multirotor .\missions\multirotor.json .\profiles\real-multirotor.json .\feedback\real-multirotor-v2.json `
  --px4-mission-feedback-evidence real vtol .\missions\vtol.json .\profiles\real-vtol.json .\feedback\real-vtol-v2.json `
  <原有四模式证据、parity、status 与输出参数>
```

`run_current_release_gate_pre8.ps1 -Px4MissionFeedbackBinding` 的等价格式是六个
`MODE|VEHICLE|MISSION_JSON|SIGNED_RUNTIME_PROFILE_JSON|REPORT_JSON` 字符串。六份 profile 与报告必须
分别是不同文件；Mission 可以按机型跨 HIL/REAL 复用。生成器与 evaluator 都会重开文件并验证
SHA-256/bytes、Mission/adapter/profile/vehicle/endpoint/source identity、反馈 freshness 与输出锁。

当前 v43b parity 文件 SHA-256 为
`B4E080286DAEA35AF55734DE1C3B0AF05427373A10F90A5E186712AF5E6E7D5B`，PX4 path hash 为
`c5cb36fed3a1c8576178a1021908529ea7d8e12dd3035a39ab4ee5bd5d1f3ccb`。v43b software-only external
join/revalidation SHA-256 为
`DC56F383B41D9042F0ED0B385762C78DC9987F1BF2C2E3E6A1FAD5625370159C` /
`C58FB2F3ACB6DF8668776C01633499385896ABE25940CD8603C0C300E9230467`；只是保存源针对当前 parity 的重算。
当前 E7 manifest/report/strict-report 为 21,038 / 54,833 / 54,833 bytes，SHA-256 分别为
`C649E62BF1E4C88DEB5CE6BE173B2987DF55826DF9799812934477981CDFF996`、
`89E9A82B4675C0DBB4F1FB36301C9F98BDC6BEBF9CE31F49808F3B470FAFD06B`、
`C609F0BA2E1B7F8E7886797FFD8A1AEE3B7812B85F72AEEDD92CF1DCB3211814`；没有伪造现场六源，故
`px4_mission_feedback.present=false`、总门关闭，strict 返回 `2`。完整回归为
`2772 passed, 1 skipped`；汇总日志 SHA-256 为
`399453AB6E33089442CDB13795F242683777C0D2A15D948AB4BF5963BDD7EA95`。真实 Pixhawk/Jetson、室外 GNSS、
HIL/REAL、实飞和人工 Fly View 仍 pending。

## v42 HIL/REAL × 三机型 PX4 Mission feedback 发布绑定（2026-09-04，`v0.3.0-pre.8`）

E7 顶层新增可选 `px4_mission_feedback` 记录。生成时必须直接提供六个来源对：HIL 与 REAL 各自的
fixed-wing、multirotor、VTOL `ExecutableMission + px4-mission-feedback-observation`。生成器和
evaluator 都会重新打开每个文件，复算文件 SHA-256/bytes、Mission identity/item count、机型
adapter、runtime mode、MAVLink source system/component、feedback/evidence hash、只读与零发送状态；
六份报告必须解析为不同文件。该记录还绑定当前 `px4_mission_path_hash`，evaluator 会再与四模式
E7 parity 比较，防止同时替换六份报告却脱离当前 Mission 实现。

采集仍使用 v41 的 receive-only CLI。采满六份报告后，将它们直接加入现有 manifest 命令：

```powershell
$env:PYTHONPATH = 'src'
python scripts\create_release_gate_manifest.py `
  --px4-mission-feedback-evidence hil fixed-wing .\missions\fixed-wing.json .\feedback\hil-fixed-wing.json `
  --px4-mission-feedback-evidence hil multirotor .\missions\multirotor.json .\feedback\hil-multirotor.json `
  --px4-mission-feedback-evidence hil vtol .\missions\vtol.json .\feedback\hil-vtol.json `
  --px4-mission-feedback-evidence real fixed-wing .\missions\fixed-wing.json .\feedback\real-fixed-wing.json `
  --px4-mission-feedback-evidence real multirotor .\missions\multirotor.json .\feedback\real-multirotor.json `
  --px4-mission-feedback-evidence real vtol .\missions\vtol.json .\feedback\real-vtol.json `
  <原有四模式证据、parity、status 与输出参数>
```

PowerShell wrapper 的等价输入是六个
`MODE|VEHICLE|MISSION_JSON|REPORT_JSON` 组成的 `-Px4MissionFeedbackBinding` 数组。最终
`v0.3.0` 必须存在且通过这条子门；prerelease 暂缺时会明确报告 `present=false` / `passed=false`，
而不是生成一份假通过。只要记录出现，任一失败在所有发布通道都属于权威阻断。

当前 v42 快照没有现场六源，因此总门仍关闭。manifest 20,955 bytes、report 54,614 bytes、strict
report 54,616 bytes；SHA-256 分别为
`9F8330CEC3D4175E314F78C00C99A3D6CCFBCE711D08F0D6C5E4A0417B859A2A`、
`8CBBD0DD0468796EDAF596C6A6359EF4A7A9D27CA32168DD1F953D5F55C8FB7E`、
`27E5A41035A6D31251F85F0913CB0B25256B15B3FD7DC762E93F07F95167BE5C`；严格 evaluator 返回 `2`。
当前 parity SHA-256 为 `C2BAC5A77719B24B9F899B278053DE5CC3526FEDB2C46371AF6D94090DDAB8E9`，
PX4 path hash 为 `9ff62c8ac9daea498732a9f5690ef195b8091dd94e621947e5d2ea3fb11500e8`。
这些只证明发布绑定代码和软件回归，不能代替真实 Pixhawk/HIL/REAL、室外 GNSS、实飞或人工 UI 验收。

## v41 PX4 Mission wire feedback 子门（2026-09-04，`v0.3.0-pre.8`）

当前源码新增 hash/source/time-bound `Px4MissionFeedbackSnapshot`，只读接收
`MISSION_CURRENT` / `MISSION_ITEM_REACHED`，最大年龄 `1.5 s`。MissionProgress 可选择要求同一
Mission、同一决策时刻的合格 snapshot；旧游标超时、来源变化、序列回退/越界、reached 超前或时间
倒退均关闭该子门。raw replay 对完全缺失的新字段继续省略，历史 recording identity 保持兼容。

现场子门通过以下 receive-only 命令采集，随后必须用同一 Mission artifact 独立重验：

```powershell
$env:PYTHONPATH = 'src'
python -m multidetect.px4_mission_feedback_cli `
  --mission .\compiled-mission.json `
  --endpoint udpin:0.0.0.0:14550 `
  --runtime-mode hil --duration-seconds 30 `
  --out .\artifacts\field\px4-mission-feedback.json
python -m multidetect.px4_mission_feedback_cli verify `
  .\artifacts\field\px4-mission-feedback.json `
  --mission .\compiled-mission.json
```

采集报告要求 `read_only=true`、`messages_transmitted=0`、Mission/source/time/hash 全部一致；它本身
仍把 release/hardware/HIL/REAL/operator visual claims 固定为 `false`。软件回归为
`2756 passed, 1 skipped`、退出码 `0`，日志 SHA-256
`079ECE9B161174A42BFEAA4B53E270E7B13C0F577CD9622759F750C25E9B9961`。当前 parity 文件 SHA-256
为 `B5FE05726534F5781995E56C85D3CA7043553B766C548B17083711FCAFF53776`。这只建立现场采集与验证所需
的契约；没有新鲜真实 Pixhawk feedback、Jetson/Pixhawk 链路、室外 GNSS、HIL/REAL 和人工 Fly View
证据时，总门继续 `gate_passed=false` / `release_allowed=false`，版本不得升为最终 `v0.3.0`。

当前 loopback/no-data 冒烟文件
`artifacts/evaluation/terminal-continuation-v41/px4-mission-feedback-report-only-smoke-20260904.json`
（SHA-256 `D34FB2237628BF7367F178E42EE8640B43EA4FEDD537FC75253A755655EED6E1`）只证明真实 CLI 能打开本地
receive-only provider、写入并重验不合格证据；其 `feedback_gate_passed=false` 和
`mission_feedback_unavailable` 必须原样保留，不能进入 HIL/REAL passed 清单。

## v35 加速度预算完整性门与当前 E7 绑定（2026-09-04，`v0.3.0-pre.8`）

目标帧与 GPS 时间对齐时，若 telemetry 给出 `acceleration_uncertainty_mps2` 但缺少完整 N/E
速度，目标投影 fail-closed，保证 `0.5·dt²·σa` 不会被静默丢弃。当前 parity 文件 SHA-256 为
`D9CF6D9E625E6B0ADB56084628CCB39ED358E2156AF89201633C2F6E1F2B9AEF`，core hash 为
`7D35526D63E3BFE247776F1ED2CC14F2950E0E61AB72E10A170C5C41BB930CF3`。

wrapper 默认绑定 v35 external join/revalidation（SHA-256
`B459F6CDA7E6BD60EE5BED486E56F25870D388E31650F54333AC2FA3741CD12B` /
`D825A2A8A1D8E3F152C35C6C8C70C3D847D9C26E3329A4F8A7EE2D27974B338C`）。当前 E7 report-only
manifest/report SHA-256 为
`B4801000CCF23B3243ADCBAA4788C00539973A0BA1A3D6D48233758782A728E4` /
`2E4CCE1FA23899958BBF05E5F399171CA462B83745D6435509BF5AEEC2FE71E7`，parity/external 子门
通过；合成 replay-set、现场硬件、人工视觉、HIL/REAL 和 prerelease channel 仍关闭总门。

## v34 运动补偿不确定度与 replay-set 校验重绑（2026-09-04，`v0.3.0-pre.8`）

> v34 是 v35 之前的历史 E7 输入，保留原始哈希与结果，不再作为 wrapper 默认输入。

目标落图新增可选 `VehicleTelemetry.acceleration_uncertainty_mps2`。恒速外推的均值保持不变，
运动误差用 `hypot(|dt|·σv, 0.5·dt²·σa)` 合并；字段必须有限且非负，live/replay 入口对非法值或缺少
完整 N/E 速度 fail-closed，旧 JSONL 缺省保持兼容。该变化只影响态势感知/证据圆，不改变 QGC wire、任务
契约或飞控/载荷输出。

历史 Gazebo raw replay 的 recording identity 因 telemetry schema 扩展而需要重算。本轮从同一
已保存 capture 生成 v34 副本，原始 `artifacts/replay-sets/sim-gazebo-2026/` 不覆盖：
`artifacts/evaluation/terminal-continuation-v34/replay-set-sim-gazebo-2026/replay-set.manifest.json`
为 4,965 bytes，SHA-256 `F577BF8085F33311D20AE27BB882C5C5F0F7225FC6F86B379A5384982E1FEDFF`，
canonical hash `F2834DC4CD03C95DC6502C137EAA66430A3F68CC2F145498889156621FA32E4C2`。六个 stratum
仍标记 `synthetic_demo`、空 truth/predictions；所以 replay accuracy 与 release gate 仍 closed。

v34 阶段 external SIM/runtime + AI join/revalidation 已按当时 canonical parity 重验通过：SHA-256
`80599422FD5D39E3445F8D3ADA5473CAC0F62D184023A2B2F2BD4CFF6E46C1FE` /
`06D4C67C5A095661F5D54A691C334C8F6D2576E5821CB365166C8CE2E28E1BAE`。v34 阶段 wrapper 无参数默认改用
v34 replay-set、accuracy/calibration 和 external 输入；report-only E7 manifest/report 位于
`artifacts/evaluation/terminal-continuation-v34/release-gate-current/`，SHA-256 为
`7C08D3254C49DB9531D1B1032939FF1D4C01426164299C0EEF212A0B6C661FBF` /
`12C8C52A9F58EEA97EC6AF863295BAAA73605CC65D1AC0B83408BD48D75156C5`。external/parity 子门
通过，但 replay-set、现场硬件、人工视觉、Mission、HIL/REAL 和 prerelease channel 仍使总门
保持 `gate_passed=false` / `release_allowed=false`；最终 `v0.3.0` 不提前发布。

## v32 导航速度残差不确定度验收与当前 E7 快照（2026-09-04，`v0.3.0-pre.8`）

`NavigationVelocityFusionEngine` 在 GPS/VIO/空速候选通过 `3.5σ` 成对一致性门后，计算
North/East 两轴残差 `χ² / (2*(N-1))`；Birge ratio 在分歧超过声明协方差时放大速度 `σ`，
追加 `navigation_velocity_uncertainty_inflated` 并保持 `DEGRADED`、`advisory_only=true`。
GPS 质量门为 `fix_type>=3`、卫星 `>=6`，`255` 未知哨兵拒绝；该路径不产生飞控输出。

软件验收记录：
`artifacts/evaluation/terminal-continuation-v32/navigation-residual-inflation-acceptance-20260904.json`
（3,351 bytes，SHA-256 `AA1E0EF13A40F9EDBD777B5D214F1601299E280DFF8E4B2B40353FF5037D47C7`），
nominal/spread 场景与 18 个专项测试通过。当前 parity
`configs/release-parity.json` SHA-256 为
`723CC760FE277DB378BB3FA55BDB0D54149F72AF38C29D2EEC67EC95BA434B4B`，core hash 为
`354d6a61f1dc986eca15e30fefff7ee820e7d83d0c3a18f4071904fc2a194d35`。

加入验收记录自身的绑定测试后，严格全量回归为 `2670 passed, 1 skipped`、退出码 `0`；日志
`artifacts/evaluation/terminal-continuation-v32/full-regression-v32-navigation-residual.log`
（3,078 bytes，SHA-256
`F1B63141BC7B6B05B897711C30EF97BB2230F779633CA71833537B62EE360656`）。

基于新 parity 的当前 Docker Desktop `28.4.0` VTOL live-AI 运行通过，源记录 SHA-256
为 `081B922CD725ACD1F42ED218F142E7AE1E8E5A684CA02F622B4AECE738E12DA`（`ai_steps=2`、
armed heartbeat、truth-free `TRACK`）；三机型六源 join
`7AA749EB13AA18A42A734D890401588B9885A7805969CEE2BAF3AB500923E392` 与独立 revalidation
`F5D1F057D85498C8E1A8BC563D950419E2D5AAAA9FDBBB28D13DF768D8A7348F` 均通过，文件位于
`artifacts/evaluation/terminal-continuation-v32/`。

当前标准 wrapper report-only E7 快照：
`artifacts/evaluation/terminal-continuation-v32/release-gate-current-20260904T070708Z/`，
manifest 20,885 bytes / SHA-256
`064FD23D88A0FA65280D4493A6AB9330748DC8D23C1FA261557DC8E7E515EBA4`，report 54,111 bytes /
SHA-256 `87D0D7F61FC802DBA66D54A592D3ED0AB30EB83115ADEBB091162D1E580D9187`。external、
scene-stream set、parity 子门为通过；总门仍 `gate_passed=false` / `release_allowed=false`，
31 条阻断保留，因 replay-set、现场 Jetson/GR01/相机与 `192.168.144.0/24` route、真实
GNSS/Pixhawk、HIL/REAL、人工 QGC Fly View 及最终 release channel 尚未完成。Docker 证据
保持 software-only，不升级为真实硬件、室外 GNSS 或实飞。

## v33 现场链路只读预检刷新与 E7 重新绑定（2026-09-04，`v0.3.0-pre.8`）

现场预检以 Ethernet、协议探测和被动 route probe 重复 2 次，接口诊断/报告结构校验通过。
当前工作站 `Ethernet=192.168.1.13/24` 不在期望 `192.168.144.0/24`，没有候选 aircraft 网卡或
目标 route；Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760`、相机
`192.168.144.108:554` 的 TCP/协议探测均超时，因此 `hardware_link_ready=false`。预检保持
只读，不登录、不请求 MAVLink stream、不启动 RTSP、不发送控制数据，也不改网卡/IP/路由。

原始报告为
`artifacts/evaluation/terminal-continuation-v33/field-link-preflight-live-20260904T-current.json`
（12,923 bytes，SHA-256
`2B3694E74332B96B3257F405842B3584A44AE07D5FCEA8EC5E47B07DF31E651A`），接口诊断为
`artifacts/evaluation/terminal-continuation-v33/field-link-interface-diagnostic-live-20260904T-current.json`
（4,412 bytes，SHA-256
`9800D930FD0FAF1D945420BF3849175F165DC463AEBD3308411143BDD99E9D68`）。用该新鲜报告重新绑定
的 E7 report-only 快照位于
`artifacts/evaluation/terminal-continuation-v33/release-gate-current/`：manifest 20,915 bytes、
SHA-256 `C66DB565A12DC664C28F4DB12E38AD7E6611E28A14F650839362A18EDB817C19`；report 54,097 bytes、
SHA-256 `340CE3C01968194B77010A808AE8A09BD62F269D9D56C786224E88099A3827D1`。结构和 freshness
通过，但总 `gate_passed=false` / `release_allowed=false`、31 条阻断保持；`v0.3.0-pre.8` 不变。

`run_current_release_gate_pre8.ps1` 的无参数默认输入已切换到 v33 现场报告；飞机接通后必须先
确认实际 Jetson/GR01/相机地址，再重跑 `run_field_link_preflight.ps1`。该报告不能替代真实
GNSS/Pixhawk、HIL/REAL 或人工 QGC Fly View 验收。

## v31 多源测距残差不确定度验收与历史 E7 快照（2026-09-04，`v0.3.0-pre.8`）

`MultiModalRangingEngine` 在通过 `3.5σ` 硬一致性门后，还会计算接受源的加权残差
`χ²/(N-1)`。当实际分歧超过声明协方差时，融合 `σ` 通过 Birge ratio 放大，并使结果保持
`DEGRADED`；`solve()` 使用 `absolute_range_uncertainty_inflated`，`solve_direct()` 使用
`direct_range_uncertainty_inflated`。放大的 `σ` 会继续进入 slant/ground `CI95` 和目标落图误差
预算，且仍然是 advisory-only，不产生飞控或物理释放动作。可重算验收记录为
`artifacts/evaluation/terminal-continuation-v31/ranging-residual-inflation-acceptance-20260904.json`
（3,165 bytes，SHA-256
`88516D2203D168C894945EDC5F1D7DFA6569F07B8DF9DD8ED910058BC27FA2DA`）。

算法源变化后重新执行了当前 Docker Desktop 的三机型 external SIM/live-AI 运行，并将六份
独立源记录用当前 parity 重新 join。Docker CLI 为
`C:\Program Files\Docker\Docker\resources\bin\docker.exe`（Server `28.4.0`）；VTOL
新鲜 AI 记录包含 armed heartbeat、truth-free 目标输入和 `TRACK` 决策（`ai_steps=2`）。
join 与独立重算复核如下：

- `artifacts/evaluation/terminal-continuation-v31/simulator-external-runtime-ai-join-20260904-current-pre8.json`：5,491 bytes，SHA-256 `0681A9BC24883DBE284793217E9DBC79C62B40A920B2372625714A53010EE1DD`，`gate_passed=true`；
- `artifacts/evaluation/terminal-continuation-v31/simulator-external-runtime-ai-revalidation-20260904-current-pre8.json`：561 bytes，SHA-256 `DDF73895D5D8E6B4E7B51670E7644972248F41B17286E32AA84507369D6C9C6E`，`source_recomputed=true`、`gate_passed=true`；
- 新鲜 VTOL AI 源记录 `artifacts/evaluation/terminal-continuation-v31/simulator-external-runtime-ai-evidence-20260904-current.json`：107,147 bytes，SHA-256 `770A0C5186582135017D23A21A6596DC36EAD1A0D3560649077F12F028D35B93`。

当前标准 wrapper 生成的 report-only E7 manifest/report 为：

- `artifacts/evaluation/terminal-continuation-v31/release-gate-current-20260904T065500Z/release-gate-pre.8-current-20260904T065500Z-v6-bound.json`：20,856 bytes，SHA-256 `CA97AE02767F93B691A3CB2B41F823FA0E53EB1D5D6B726C657D086861F523E1`；
- `artifacts/evaluation/terminal-continuation-v31/release-gate-current-20260904T065500Z/release-gate-pre.8-current-20260904T065500Z-v6-bound-report.json`：54,109 bytes，SHA-256 `2E66751A6D7E0DEB16276431C5F9DECE74969D783C3EC78461EDD95FE0EC9B6E`。

该快照已绑定 core parity hash
`4ed386e9eaf91952b7c7be56d3d272efeaca4bb70a50e78ed57e04aa5307f5ad`，external SIM 和
scene-stream set 子门均为 `passed`。总门仍为 `gate_passed=false`、`release_allowed=false`，
保留 31 条阻断：现场 Jetson/GR01/相机与 `192.168.144.0/24` route 不可达
（`hardware_link_ready=false`），以及 replay-set review/accuracy、真实 GNSS/Pixhawk、
HIL/REAL、人工 QGC Fly View 和最终 release channel。external 记录只证明 software-only
PX4/Gazebo/AI 进程和共享协议门，不升级为硬件、视觉或实飞验收。当前绑定的现场只读
preflight 为 `artifacts/evaluation/terminal-continuation-v31/field-link-preflight-live-20260904T145200Z.json`
（18,174 bytes，SHA-256 `04FD7C8C13C39369FF75203223BDDBF97FDC48C5D16C30CDC81D31A3230A66A5`）；
报告 freshness 通过，但端点与 route 探测失败，readiness 仍为 false。
本轮严格全量回归为 `2668 passed, 1 skipped`、退出码 `0`，日志
`artifacts/evaluation/terminal-continuation-v31/full-regression-v31-external-rebind-20260904.log`
（3,078 bytes，SHA-256 `837E9E87D8B5F40EB93001BDB8F674CF151B97D0E4DEFD0DD291934FD15A86F8`）。

## 三机型 scene-stream set 接入（2026-09-04，`v0.3.0-pre.8`）

scene-stream set 已接入正式 E7 manifest，而不是只作为旁路 SIM provenance。生成清单时可重复
传入三条源流：

```powershell
$py = 'D:\Projects\Multi-Detect\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\create_release_gate_manifest.py `
  --out artifacts\evaluation\release-gate-pre.8-scene-stream-bound.json `
  --parity-json configs\release-parity.json `
  --replay-evidence <replay.json> `
  --sim-evidence <sim.json> `
  --hil-evidence <hil.json> `
  --real-evidence <real.json> `
  --simulator-scene-stream-set-evidence <fixed-wing.scene-stream.json> `
  --simulator-scene-stream-set-evidence <multirotor.scene-stream.json> `
  --simulator-scene-stream-set-evidence <vtol.scene-stream.json> `
  --allow-incomplete
```

`--sim-scene-stream-set-evidence` 是同一参数的短别名。Windows
`scripts/run_current_release_gate_pre8.ps1` 默认也传入当前三条源流；需要更换源流时显式提供
`-SimulatorSceneStreamSetEvidence <path1>,<path2>,<path3>`。writer 要求恰好三份、每种机型一份，
先调用单流 verifier，再绑定 scenario/version、release label、canonical/source SHA-256、文件
bytes 和 snapshot 数。evaluator 读取清单时会重新打开所有源文件并重算同一组 checks；未知字段、
路径/哈希/版本漂移、重复机型或源流篡改都会将显式 binding 置为 `passed=false`。最终 `release`
channel 要求该 binding 存在且通过；pre.8 缺失仍保持向后兼容。该门只证明 truth-free
software/SIM scene-stream provenance，不替代外部 Gazebo/PX4、QGC 人工视觉、真实 GNSS/
Pixhawk/Jetson/相机、HIL、REAL 或实飞。

当前 bound manifest/report：

- `artifacts/evaluation/terminal-continuation-v30/release-gate-continuation-20260904T133400Z/release-gate-pre.8-current-20260904T133400Z-v6-bound.json`：20,828 bytes，SHA-256 `98A301DA0147010CBF6F927122304E3AE1F47B08B095B8E9D89D535A2C37547C`。
- `artifacts/evaluation/terminal-continuation-v30/release-gate-continuation-20260904T133400Z/release-gate-pre.8-current-20260904T133400Z-v6-bound-report.json`：52,806 bytes，SHA-256 `1A8B3A8402132B02788217787D564B32F87E2552506152232843B8D528C61EE8`。

该报告中 `simulator_scene_stream_set.present=true`、`passed=true`，3 条记录与 9 项 checks
全通过；总 `gate_passed=false` / `release_allowed=false`，仍保留 31 条 replay-set、现场、人工
视觉、Mission、HIL/REAL 和 prerelease channel 阻断原因。结构化摘要为
`artifacts/evaluation/terminal-continuation-v30/scene-stream-set-release-gate-acceptance-20260904-v3.json`
（2,858 bytes，SHA-256
`3FAAEF71A53E303B56EFBA550EDCD57A41C704894F860FA6DD3D080EF7D81214`；v3 绑定本次 3/3 现场样本，
保留 v1/v2 历史，专项回归计数为 85）。专项 `85 passed`；
全量严格回归 `2667 passed, 1 skipped`，退出码 `0`，最终日志
`artifacts/evaluation/terminal-continuation-v30/full-regression-scene-stream-set-release-gate-final-20260904.log`
（3,700 bytes，SHA-256
`BEB6A74BC7CCB083F3B7AD3D93A99F0932FD9850D0E1684D5057F8BB45C4F8BF`）。

绑定时使用的新鲜现场只读预检为
`artifacts/evaluation/terminal-continuation-v30/field-link-preflight-continuation-20260904-133250.json`
（17,366 bytes，SHA-256
`F5E12FB3A202F87B0102FBBB0DEE122538D1328FEB292CCDF4FE68E4ADAF4B1F`）。本次请求并收集 3/3 个样本，
Jetson/GR01/相机仍不可达，route 源地址仍为 `192.168.1.13`，`hardware_link_ready=false`；真实链路和最终
`v0.3.0` 仍未关闭。

## 运行身份与资源关闭边界（2026-09-04，`v0.3.0-pre.8`）

共享 `AutonomyRuntime` 在消费任何观测前快照化 `RuntimeDescriptor.canonical_hash` 和
backend `input_hash`，处理收尾时重新校验；descriptor 或输入流在运行期间被替换会直接
fail-closed，报告不会采用结束时的可变身份。iterator 创建本身抛错时也会进入 cleanup，
确保 REPLAY、SIM、HIL、REAL 的长生命周期 launcher 不遗留半关闭 backend。回归证据：
`artifacts/evaluation/terminal-continuation-v30-full-regression-20260904-provenance-both-final.log`
（`2657 passed, 1 skipped`，退出码 `0`，SHA-256
`BA4EF43EE6E6EA08FB8D27D44600EF019C2DF86819AF1AA0A7102296AA42F2C6`）。该本地门只证明
运行契约和资源生命周期，现场 GNSS/Pixhawk、HIL/REAL、人工 Fly View 及最终 `v0.3.0`
发布状态保持原样。

对应结构化摘要为
`artifacts/evaluation/terminal-continuation-v30/runtime-provenance-acceptance-20260904.json`
（1,939 bytes，SHA-256
`FCA1733F94EDBCFCFD31CB612955595A5EBE4E38105235A2637E9BAEDE906654`），明确记录
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending` 和
`release_allowed=false`。

同一时段用默认 pre.8 输入生成的 report-only 清单为
`artifacts/evaluation/terminal-continuation-v30/release-gate/release-gate-pre.8-current-20260904T115800Z-v6-bound.json`
（18,010 bytes，SHA-256
`402565280E94540A5F5A2E84DE3DBE7F45F3AA3E5B0AD825936BD64B0D60DFC`），对应报告为
`artifacts/evaluation/terminal-continuation-v30/release-gate/release-gate-pre.8-current-20260904T115800Z-v6-bound-report.json`
（48,589 bytes，SHA-256
`E2014A9B6C992057D257899551CF35E2418BD97942DA5C6201B89EC8E6CA3794`）。两者均生成成功，
但 `gate_passed=false`、`release_allowed=false`，32 条阻断原因仍原样保留；这证明 runtime
身份收口没有替 replay-set、现场硬件、人工视觉、Mission、HIL/REAL 或 release channel
门偷换结论。

为避免把旧现场报告的时间过期误当作当前接线事实，随后重新执行只读 watch preflight：
`artifacts/evaluation/terminal-continuation-v30/field-link-preflight-live-20260904T120100Z.json`
（18,177 bytes，SHA-256
`3F9D1F06FA6021DA9228213B076D12169D58865491324983585A4D3F8EE4FB48`），接口诊断
`field-link-interface-diagnostic-live-20260904T120100Z.json` 为 4,411 bytes，SHA-256
`6D801CA49A4DC3C7F7BC21DB779B0C66F58C72C7DF5D94CF45DDB665BA083458`。3/3 samples 的结构
verifier 退出 `0`、freshness 通过；但 Jetson/GR01/相机仍全部超时，route 源地址均为
`192.168.1.13`，`hardware_link_ready=false`。绑定这份新鲜报告的 E7 manifest/report
分别为 17,922/48,467 bytes，SHA-256 分别为
`99CF86290FDEE4FFCA82FF24B4BB458A7C325F6BBDAAA878166DA518E37E8686` /
`AE30959E4366CD069804FD33651ABB4EC52E5892BEC9719D74ABC13730CCA425`；stale 原因已去除，
但 31 条其他真实阻断原因仍使总门保持关闭。

现场 preflight、E7 wrapper 和 runtime backend 的专项回归为 `66 passed`、退出码 `0`；日志
`artifacts/evaluation/terminal-continuation-v30/field-runtime-release-focused-v2-20260904.log`
为 101 bytes，SHA-256 `0651EAAEBAB70DBE258889DDADA0AFDAFE4C77BEA1F38F840369B234D1A02232`。

## 当前现场链路只读刷新（2026-09-04，`v0.3.0-pre.8`）

随后又执行单样本协议/route 预检：
`artifacts/evaluation/terminal-continuation-v30/field-link-preflight-terminal-20260904-125150.json`
（4,570 bytes，SHA-256
`D3511368A646821965A6B6A7E16895E5547FCCE964CDB57CD1AB28B0C1347C78`）。
`verify_field_link_preflight.py --allow-unready` 的结构校验退出 `0`，但
`hardware_link_ready=false`：Jetson/GR01/相机的 TCP 与协议探测均超时，三个 route probe 均选中
`192.168.1.13`，不匹配期望的 `192.168.144.0/24`；本地 UDP 端口可用。该快照只刷新当前网卡
和端点事实，不关闭现场/GNSS/HIL/REAL 门，也不产生飞控指令。

## 现场 route 源地址诊断（2026-09-04，`v0.3.0-pre.8`）

`build_field_link_report()` 在启用 `--expected-source-cidr` 或 `--require-route` 时，
会在 `summary.route_source_addresses` 写入每个被动 route probe 的实际源地址。该字段只是
`route_probes` 的确定性摘要：`verify_field_link_preflight.py` 会重新投影并比较端点名称、
地址和值，历史 v1 报告没有该字段时仍保持可读。它用于快速识别迁移后 VPN/TAP/错误网卡
抢占路由的情况，不能把不可达的 Jetson、GR01 或相机升级成 `hardware_link_ready`。

当前台式机的最新只读报告
`artifacts/evaluation/terminal-continuation-v29/field-link-preflight-live-20260904e.json`
（3,886 bytes，SHA-256
`8A5DF5874723819A2B5754061A42C682C7230A51CCCCA1E9AD55A59EEBD54A73`）记录三个端点都选中
`192.168.1.13`，而预期飞机网段是 `192.168.144.0/24`；结构 verifier 在
`--allow-unready` 下退出 `0`，报告自身仍为 `hardware_link_ready=false`。接线复核时先看
该映射，再重新运行接口快照和三样本 `-RequireRoute -RequireReady` 预检。

该字段接线后的严格源码回归为 `2654 passed, 1 skipped`，退出码 `0`；唯一跳过是 Windows
符号链接权限。原始日志为
`artifacts/evaluation/terminal-continuation-v29/full-regression-route-source-wrapper-final-20260904.log`
（3,521 bytes，SHA-256
`20BD8EF8F756B47224CEB090BC985F5DDAF2E7E9901D87C1518CEEAD292A421F`）。

两样本 watch 复核文件
`artifacts/evaluation/terminal-continuation-v29/field-link-preflight-watch-route-source-20260904.json`
（10,539 bytes，SHA-256
`C259273466907B4D2FF4188C0CA582BA13FD18CF3102205CC5D7D1B2EEFB8831`）也已由保存报告
verifier 重新打开；结构门退出 `0`，但两次采样均选中 `192.168.1.13`，聚合
`hardware_link_ready=false`，所以没有改变现场或最终发布门。

为减少迁移台式机时的手工步骤，Windows wrapper 新增
`-GenerateInterfaceDiagnostic [-InterfaceDiagnostic <path>]`。它会在同一轮预检中调用
只读 `inspect_field_link_interfaces.ps1`、生成最新快照并将其 SHA-256 绑定到报告；不传
该开关时，已有 `-InterfaceDiagnostic` 的旧路径保持不变。实跑绑定报告
`artifacts/evaluation/terminal-continuation-v29/field-link-wrapper-generated-diagnostic-20260904.json`
的 verifier 退出 `0`，接口快照的 `selection.status=missing` 与三个端点源地址
`192.168.1.13` 均被保留，`hardware_link_ready=false`。

## 模型审核 envelope 与 artifact 绑定（2026-09-04，`v0.3.0-pre.8`）

模型精度报告现在可以通过 `evaluate_replay_set.py --model-artifact` 记录运行时使用的
ONNX/TensorRT artifact SHA-256，并可同时绑定 model ID/version。新的
`scripts/create_model_review_bundle.py` 会重新验证 fire-candidate 的 post-NMS `N x 6`
manifest、`model-check` 输出、部署域六层 replay accuracy 和四类独立治理审核，生成只读
审核 envelope；它不修改 manifest，也不授予飞控或物理载荷权限。

已有 synthetic HIL 模型的诊断 envelope
`artifacts/evaluation/terminal-continuation-v29/model-review-synthetic-20260904.json`
明确为 `production_approval_eligible=false`，因为 `status=synthetic_hil` 且没有真实
部署域证据。该文件的 SHA-256 为
`52C72DB0C562D7317E3BEBE85E6A8E8E69DB63B0C68414F7024B093B2C487002`；它只证明审核工具的
fail-closed 行为，不能关闭 REPLAY/SIM/HIL/REAL 或最终 `v0.3.0` 门。

### 将模型审核绑定到 E7 清单

模型审核 envelope 生成后，使用 `create_release_gate_manifest.py --model-review-bundle`
把它作为顶层 `model_review` 记录接入同一份四模式清单。writer 会拒绝符号链接、重算
bundle SHA-256 并重新验证 manifest、模型 artifact、model-check、部署域 accuracy 和四类
治理审核；`check_release_gate.py` 读取清单时会再次执行相同的复核。因此只复制
`production_approval_eligible` 或 `gate_passed` 字段不能伪造通过状态。

在现有四模式 evidence/parity 命令中追加以下参数即可生成预发布诊断清单：

```powershell
$py = 'D:\Projects\Multi-Detect\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\create_release_gate_manifest.py `
  --out artifacts\evaluation\release-gate-pre.8-model-bound.json `
  --parity-json configs\release-parity.json `
  --replay-evidence artifacts\evaluation\replay-acceptance.json `
  --sim-evidence artifacts\evaluation\sim-acceptance.json `
  --hil-evidence artifacts\evaluation\hil-acceptance.json `
  --real-evidence artifacts\evaluation\real-acceptance.json `
  --model-review-bundle artifacts\replay-sets\fire-smoke-2026\model-review.json `
  --allow-incomplete

& $py scripts\check_release_gate.py `
  artifacts\evaluation\release-gate-pre.8-model-bound.json `
  --version-manifest configs\product-version.json `
  --report-only `
  --out artifacts\evaluation\release-gate-pre.8-model-bound-report.json
```

Windows wrapper 等价参数为
`scripts/run_current_release_gate_pre8.ps1 -ModelReviewBundle <model-review.json>`；wrapper
无参数时固定读取当前 v32 parity-valid 的 external SIM join/revalidation 与 v33 现场 preflight，
v29/v31 和 `20260903-current-provider-fallback-parity` 快照只保留为历史证据，不会被默认选用。

`--allow-incomplete` 只允许把尚未就绪的预发布证据写入清单；它不会把 pending 或
synthetic 模型变成通过。最终 `release` channel 要求 `model_review` 存在、源 bundle
可重算、`production_approval_eligible=true` 且该记录的 `gate_passed=true`，同时仍要满足
REPLAY/SIM/HIL/REAL、现场 GNSS/硬件、Mission 和人工视觉等其他发布门。审核包中的
`production_approved`、`flight_control_enabled` 与 `physical_release_enabled` 固定为
`false`，模型审核本身不授予飞控或物理载荷权限。

模型审核接线后的当前源码严格回归为 `2653 passed + 1 skipped`，退出码 `0`；唯一跳过是
Windows 账户没有创建符号链接的权限。带完整 pytest 汇总的日志为
`artifacts/evaluation/terminal-continuation-v29/full-regression-model-review-wrapper-v2-summary-20260904.log`
（452,125 bytes，SHA-256
`DCE90748ABBFD8E1A370D8CE285D603C9BFEC9BDAA23A0EAED9DBAF9671CD0FC`）。这只证明源码和
审核器回归通过，不改变 synthetic/现场硬件/GNSS/HIL/REAL 或最终 `v0.3.0` 发布门状态。

Windows wrapper report-only smoke 目录为
`artifacts/evaluation/terminal-continuation-v29/wrapper-model-review-smoke-b2a16a4739ad4ed79a655893500ddf5d/`；
manifest/report SHA-256 分别为
`A71D4384550A551D229A06FD36E78149C2FD02E2ACD0994A8510EC529B23A70F` 和
`887FBB62121CCE48A30A9CC65C2127D68D6AC4CCC15775F234CF77301B563B81`，退出码 `0`，但
`model_review.passed=false`、总门仍为 `gate_passed=false`。

## v29 正向 replay evaluator 诊断夹具（2026-09-04，`v0.3.0-pre.8`）

本轮在独立目录
`artifacts/evaluation/terminal-continuation-v29/replay-set-positive-synthetic/` 生成了只用于
评估器接线的正向 fixture。manifest 为 4,975 bytes、SHA-256
`61E69B979C33F88B1E044F6192292E2CC55BF5EF6928645A7F28E16F5172BF46`；五个正例 strata 的
真值/预测均存在，aggregate IoU precision/recall 可重算为 `1.0/1.0`。

显式 `--reviewed --reviewer qa-team` 的 evaluator report 为 12,649 bytes、SHA-256
`6EF07FAFE7BA56E696498A856532BF46FE35227F22ADDC5C048FE4DE8C8048F2`；readiness report 为
10,940 bytes、SHA-256 `1EB519C6A5350007973742593A60F669F94AECFD646E2CEED5730BD4E507C13C`。
复核只确认 evaluator 的正例分层与哈希重算，不能改变来源属性：条目仍为
`source_kind=synthetic_demo`，没有部署域录像，标注也未独立复核，所以
`deployment_domain_evidence_complete=false`、`accuracy_validated=false`、`gate_passed=false`
是预期结果。该 fixture 不绑定 canonical replay-set，不能关闭 REPLAY/SIM/HIL/REAL 或最终
`v0.3.0` 发布门。

## v26 三机型 deterministic SIM 复核（2026-09-04，`v0.3.0-pre.8`）

在不调用桌面控制、不启动外部 PX4/Gazebo 的条件下，重新执行三种机型的共享
`ManualClock`、runtime backend、AI、authority、watchdog 和故障尾段矩阵。证据为
`artifacts/evaluation/terminal-continuation-v26-scenario-matrix.json`，文件 13,300 bytes，
SHA-256 `06C8E829CCA2321C5D10F2F700E04D2469772E0C9C6E7711738B160DBC110FC1`；独立
verifier 退出码为 `0`。fixed-wing、multirotor、vtol 三个场景均完成 2,611 帧，故障覆盖率
为 `1.0`，重复 hash 一致，`gate_passed=true`、`deterministic_gate_passed=true`。

同一轮 native-QGC interactive fixture 证据为
`artifacts/evaluation/terminal-continuation-v26-interactive-acceptance.json`，文件
113,273 bytes，SHA-256
`041B882EA4A9C3CB44A3AF2A74E61D3F216C99C4D3AE24B924D74E300BBFBAB8`，三机型共享
AI/manual authority、watchdog、loopback/identity 和 Mission write 阻断均为通过。

两份证据都保持 `truth_free`/software-only 边界；`external_3d_runtime`、人工 QGC
Fly View、真实 Pixhawk/Jetson/GNSS、HIL、REAL 和最终发布门仍为 `not_claimed` 或
`offline_pending`，不能把该轮结果写成最终 `v0.3.0` 通过。

v26 全量 Python 回归日志为
`artifacts/evaluation/terminal-continuation-v26/full-regression.log`，结果
`2624 passed + 1 skipped`、退出码 `0`，SHA-256
`A70388821B0E7A4B8BE782162681BD3FDEFB5F710F40701A8A24C08617BC596E`。

## v25 现场链路 freshness-bound wrapper 复核（2026-09-04，`v0.3.0-pre.8`）

v24 总验收之后，使用新的现场 watch 报告重新执行当前 wrapper，以确认清单的 freshness
字段不能只靠历史输入维持。现场报告为
`artifacts/evaluation/terminal-continuation-v25/field-link-preflight-live-20260904a.json`
（16,808 bytes，SHA-256
`53E45C0C4D7B6EB7942BB7438A90F29BC422893E3E6259F97A8A4C33EF74AB39`）。输出目录为
`artifacts/evaluation/terminal-continuation-v25/e7-freshness-bound-900/`：manifest
17,902 bytes，SHA-256
`6C854C43E932FF4CC010B099A44ACED2CF62749C3A86D8405F27C6CFB6271CA4`，report 47,074 bytes，
SHA-256 `E192B4E7BED44B2B13660F58A019CF2CCAE572DB12D8C2F2012E4FCC67AD6B84`。

wrapper 显式传入 `-RealFieldLinkMaxAgeSeconds 900`；manifest 同时写入
`max_age_s=900` 与源 `report_timestamp_epoch_s`，schema 校验为 `MANIFEST_SCHEMA_ERRORS=0`，
绑定时报告年龄约 `157.1 s`。本次 report-only wrapper 退出码为 `0`，但
`hardware_link_ready=false`、`gate_passed=false`，因为 Jetson、GR01/Pixhawk、相机、期望网段、
REPLAY 和人工/硬件验收门仍未通过；这份证据只证明 freshness 绑定和 fail-closed 行为。
随后全量回归收集 `2625` 项，结果为 `2624 passed + 1 skipped`（Windows symlink 权限）。日志
`artifacts/evaluation/terminal-continuation-v25/full-regression-final.log` 为 3,492 bytes，
SHA-256 `74CCDE8D3C72276B8122DFB03579D1E76CAF98AAD0F5030F6F7AEB7DD3CDC0B7`，ruff 与
compileall 均返回 `0`。

同一 v25b 报告又被完整 goal-acceptance 重新绑定，快照
`artifacts/evaluation/terminal-continuation-v25/goal-acceptance/multidetect-goal-acceptance.json`
为 46,791 bytes，SHA-256
`4514AF0836C0E33117475B3BD1C272F24D94A857B215C253BAFE05CFCE16F5D7`；日志为 58,812 bytes，
SHA-256 `78342FAA5DDC8B43BBDDDC1EAC986A1F494F693E87AFED36366479CE03A56F59`。快照确认
`software_status=pass`、核心 `2625`、QGC custom `66`、三机型 SIM/external 子门通过；
`hardware_status=offline_pending`、现场 `hardware_link_ready=false`，REPLAY、真实设备、
人工 Fly View、HIL/REAL 和最终 release 仍被阻断。

同一 v25b 报告随后再次绑定到独立 E7 wrapper 输出目录
`artifacts/evaluation/terminal-continuation-v25/e7-freshness-bound-900-b/`。manifest SHA-256
为 `7946161569981FD367820A54C45D7EDA51EDB8D9FDF581D7921089DEB4560CA7`，report SHA-256 为
`79EB13C0B1132E81D48E86DB6EAF2ED2A7CAF320044A29F63005429D1379AC3D`；两个 freshness 字段
均存在，wrapper 退出码为 `0`，但 `gate_passed=false`，现场链路仍保持 fail-closed。

## goal-acceptance v24 freshness-bound 快照（2026-09-04，`v0.3.0-pre.8`）

本轮将 REAL 现场链路绑定升级为 freshness-bound，并重新执行完整验收。快照
`artifacts/evaluation/terminal-continuation-v24/multidetect-goal-acceptance.json` 为
46,146 bytes、SHA-256
`5E5820365FD95E8BBC3987E8559A88878922CC3E3CD478D3A60AB6272C53E3C3`；原始日志为
58,167 bytes、SHA-256
`CB4541FEAF9030022B4ADDAB7D7BEF9F06E4A9073E9D671D5399EB0860B33BF7`。软件门、核心收集
`2624`、QGC custom `66` 和三机型确定性 SIM 子门通过；现场 freshness 约 `27.2 s`，
窗口 `900 s`，但 `hardware_link_ready=false`、`hardware_status=offline_pending`，
六项 live gates 继续阻断。

对应 watch 报告为
`artifacts/evaluation/terminal-continuation-v24/field-link-preflight-live-20260904e.json`
（16,810 bytes，SHA-256
`2341C97D1146CC33D0734D7947A5A543D05A6D96C7E6C8B0896E1C974EA6B5F2`），离线 verifier
为 `artifacts/evaluation/terminal-continuation-v24/field-link-verification.json`
（1,799 bytes，SHA-256
`C489A288FA478E01C980238149FB26FDD0BE81BBC7A50C2E6B0FF94A356DFB6F`）。重点回归
`final-regression-summary.log` 为 `276 passed`，SHA-256
`B9424480C9EFC6120221B7F47DD9C325A17048A8FD6528936ADAB01F4B215C2F`。

移除旧 pytest 的重复 `pythonpath` 配置后，独立全量回归日志
`artifacts/evaluation/terminal-continuation-v24/full-regression-post-wrapper-no-warning.log`
退出码为 `0`，结果为 `2624 passed + 1 skipped`（唯一跳过项为 Windows symlink 权限），
3,477 bytes，SHA-256
`0CB6EE557B7529E0124540D5A1B37513B8EA2CEAB9A1F17C9B59D973310D7EAA`；ruff 与 compileall
也返回 `0`。剩余 deprecation warning 来自 pytest 6.2.5 的 Python 3.12 兼容层。

## goal-acceptance v23 最新 freshness 快照（2026-09-04，`v0.3.0-pre.8`）

freshness verifier 增加缺失时间戳回归后，当前台式机重新生成 watch 现场报告并执行完整
验收。快照
`artifacts/evaluation/terminal-continuation-v23/multidetect-goal-acceptance.json` 为
46,003 bytes、SHA-256
`ABB54E58F04C12330E37B3F2574DF73979B8C6224DC8D8F5EDDEF9BDE2D35DEB`；日志
`artifacts/evaluation/terminal-continuation-v23/goal-acceptance-rerun.log` 为 58,072
bytes、SHA-256 `D26E844E778D53AB926077935E8980464C565109CCA9445DF4859B0D90F68762`。
快照记录 `software_status=pass`、核心收集 `2623`、QGC custom `66`、三机型确定性
3-D SIM 子门通过，现场 freshness 约 18.3 秒且在 900 秒窗口内；设备端点仍不可达，
`hardware_status=offline_pending`、`field_link_ready=false`，六项 live gates 继续阻断。

## field-link freshness 绑定与 goal-acceptance v22（2026-09-04，`v0.3.0-pre.8`）

现场预检 verifier 新增可选时间窗口：one-shot 以 `generated_at_epoch_s` 为观察时刻，watch
以 `finished_at_epoch_s` 为观察时刻。缺失、未来或超过 `max_age_s` 的报告会被拒绝；manifest
中的 `max_age_s` 与 `report_timestamp_epoch_s` 必须成对出现，避免半绑定；该校验只约束
证据新鲜度，不改变 TCP/协议/route 探测结果。`run_goal_acceptance.ps1` 默认通过
`-FieldLinkMaxAgeSeconds 900` 调用该窗口。

当前台式机先生成 watch 报告
`artifacts/evaluation/terminal-continuation-v22/field-link-preflight-live-20260904c.json`
（16,807 bytes，SHA-256
`0F211E23FD0C2671C8424C276E41993EB7DABB15059A4585A044B000D3F1D682`），再用
`--allow-unready --max-age-s 900` 离线复核，输出
`field-link-verification.json`（1,797 bytes，SHA-256
`4E58BF188B0D99D10CBCCFE1DBCA18E0A04B4E655E7BE2541A2D1090F50BFF94`）；报告 age 约
20.5 秒、freshness 通过，但设备端点仍不可达，`hardware_link_ready=false`。
重新执行的完整快照
`artifacts/evaluation/terminal-continuation-v22/multidetect-goal-acceptance.json` 为
46,003 bytes、SHA-256
`B2569EAF162AD9E33266DE25ABC1D61EEFB7A214C9021807A02A80C7078CD8B2`，日志为 58,072
bytes、SHA-256 `4F57F20702EC1CDE80228C66DB2D4BD6E03C0B2C2A9ACCB20058590C0CE70D35`；
`software_status=pass`、核心收集 `2622`、QGC custom `66`，而
`hardware_status=offline_pending`、六项 live gates 继续阻断。

## goal-acceptance v21 当前台式机快照（2026-09-04，`v0.3.0-pre.8`）

三机型 3-D SIM 控制绑定之后，在当前台式机重新执行完整
`scripts/run_goal_acceptance.ps1`。快照
`artifacts/evaluation/terminal-continuation-v21/multidetect-goal-acceptance.json` 为
45,234 bytes、SHA-256
`78498FAA088110E49B640BF3BA605B23B133142BC10AAFC6B184E1091F53D089`；原始日志
`artifacts/evaluation/terminal-continuation-v21/goal-acceptance-rerun.log` 为 57,303
bytes、SHA-256 `F23336F60FE289A63DDAE65E9551180A5B594B28EE4924FD2181352D1913292F`。
快照记录 `software_status=pass`、核心测试收集 `2619`、QGC custom `66`、QML parse、
协议 self-test 和当前三机型确定性 3-D SIM 证据均通过；现场网卡仍不在飞机网段，
`hardware_status=offline_pending`、`field_link_ready=false`，六项现场/REPLAY live gates
继续阻断。该快照不替代外部 PX4/Gazebo、室外 GNSS、人工 Fly View、HIL、REAL 或实飞，
最终 `v0.3.0` 发布门仍关闭。

## 三机型 3-D SIM 控制绑定（2026-09-04，`v0.3.0-pre.8`）

本轮重新运行仓库自带的 VTOL、固定翼、多旋翼 3-D 控制资产预检，并使用
`scripts/verify_simulator_3d_control_evidence.py --bind` 生成三机型哈希绑定。绑定快照
`artifacts/evaluation/terminal-continuation-v20/simulator-3d-control-binding.json` 为
2,284 bytes、SHA-256
`967C162D4B996C4CA709B678732A8690FFA18256B6AD958CDCC08EE6CAC61B4E`；其检查项
`all_airframes=true`、`all_deterministic_gate_passed=true`、`records_hash_bound=true`，
三种 mode 各出现一次。该门只证明共享控制契约、SDF 资产和运行身份的确定性绑定，
`external_runtime_acceptance=not_claimed`，因此不替代外部 PX4/Gazebo、人工 Fly View、
HIL、REAL 或实飞验收。

## goal-acceptance v19 重新绑定（2026-09-04，`v0.3.0-pre.8`）

构建 wrapper 的退出码与 SHA-256 收口后，在当前台式机重新运行完整
`goal-acceptance`。最新快照
`artifacts/evaluation/terminal-continuation-v19/multidetect-goal-acceptance.json` 为
45,234 bytes、SHA-256
`5C6E0618F0C25A04A52832F7B00CB5113EF21BC3C2C1375F658899A8B2A04066`；原始日志
`artifacts/evaluation/terminal-continuation-v19/goal-acceptance-rerun.log` 为 57,255 bytes、
SHA-256 `CF2EC57CE361AD811733986F96BA7606005EF4BE347E80BE0F78E444FA4EC392`。快照中
`software_status=pass`、核心测试收集 `2619`、QGC custom `66`；网络仍为
`field_link_ready=false`、`hardware_status=offline_pending`，六项现场/REPLAY live gates
继续阻断，因此最终 `v0.3.0` 发布门仍保持关闭。

## Windows build wrapper 哈希收口（2026-09-04，`v0.3.0-pre.8`）

`scripts/build_multidetect_windows.ps1` 现在用 .NET `SHA256::Create()` 流式计算 Release EXE
指纹，不依赖可选 PowerShell 哈希模块。Windows PowerShell 5.1 完整运行
`-SkipConfigure -BuildSelfTests -InstallPortable -Parallel 4` 返回退出码 `0`，日志
`artifacts/evaluation/terminal-continuation-v18/build-wrapper-after-sha256-fix.log` 为 142,536
bytes、SHA-256 `F28147F8FEBEF709936A31BEFD88EA7B6243AB294A00C1CF447E9AFBD2752E23`，包含
`PORTABLE_OUTPUT`、`BUILD_OUTPUT`、Release SHA-256 和 portable/helper 路径，未出现哈希命令错误。
同一命令在 pwsh 宿主下也返回 `0`，对应日志
`artifacts/evaluation/terminal-continuation-v18/build-wrapper-pwsh-after-sha256-fix.log` 的大小和
SHA-256 与上面完全一致。
同轮 canonical QGC venv 全量回归日志为
`artifacts/evaluation/terminal-continuation-v18/pytest-full-venv-after-build-wrapper-fix.log`
（2,997 bytes，SHA-256
`4FBA774CEB187D57C2C27E7A50F03B25502352B395D014B97BC512293CA5C616`），退出码 `0`。
Release 与 canonical portable 主 EXE 均为 46,575,616 bytes、SHA-256
`4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40` 且字节一致；RTSP helper
为 95,744 bytes、SHA-256 `DE94D5404BEF48B80919BFC06465BD53EBFD50A8B65A50F38DA3BCDDADB58312`。
构建后的 portable `--offline --simple-boot-test` 退出码为 `0`，日志
`artifacts/evaluation/terminal-continuation-v18/qgc-simple-boot-after-sha256-fix.log` 为 220
bytes、SHA-256 `946D08771ACF7DE07C43218D395CC514F16CDB253A34C9AC5D4601F70299BEA9`。
这是构建 wrapper 的宿主兼容性修复，不改变任何硬件、室外 GNSS、HIL/REAL 或最终版本门。

## 台式机现场链路 live watch（2026-09-04，`v0.3.0-pre.8`）

使用显式 `Ethernet` 运行 3 样本只读 watch（协议检查、路由检查、2 秒超时），报告
`artifacts/evaluation/terminal-continuation-v18/field-link-preflight-live-20260904b.json` 为
16,807 bytes、SHA-256 `AC62A886C3723A5209D9C4F441537C4D7D82B0AC530DF90AE52D340F02B7238A`；
wrapper/verifier 退出码均为 `0`，但三次 Jetson SSH、GR01 MAVLink、相机 RTSP 均超时，路由
源 `192.168.1.13` 不在 `192.168.144.0/24`，本地 `14580/14581` 可绑定，所以
`all_passed=false`、`hardware_link_ready=false`。原始日志为
`artifacts/evaluation/terminal-continuation-v18/field-link-preflight-live-20260904b.log`，
18,533 bytes、SHA-256 `362095D6D841613B338CF8268445A3C3FCB3D596152261FDFCD10B54D4475C92`。
带 `-AutoSelectInterface` 的同网段尝试在没有活动地址时按设计退出 `1` 且不写假报告；显式
接口路径随后完成诊断。该结果只更新现场事实，不关闭硬件、GNSS、HIL/REAL 或最终版本门。

## goal-acceptance wrapper 原生退出码收口（2026-09-04，`v0.3.0-pre.8`）

`scripts/run_goal_acceptance.ps1` 增加 `Get-NativeExitCode`，把 Python、Git、QGC 测试和各类
验收 helper 的原生退出码读取统一收口。PowerShell `Set-StrictMode` 在嵌套宿主或空输出管道
下可能没有自动创建 `$LASTEXITCODE`；helper 对“变量缺失”使用 `0`，对已有值进行整数化并在
异常时返回失败码 `1`，所以不会吞掉真正的命令失败。该修复只影响 wrapper 的宿主兼容性，
不改变任何证据内容或 `-Require*` 门槛。目标脚本静态回归、AST 解析和全量 Python 回归已通过。
系统 Python fallback 日志保持 `2617` 项（`2616 passed / 1 skipped`）；与 goal-acceptance
相同的 canonical QGC venv 最新日志为
`artifacts/evaluation/terminal-continuation-v18/pytest-full-venv-after-goal-exitcode-v2.log`
（2,997 bytes，SHA-256
`4FBA774CEB187D57C2C27E7A50F03B25502352B395D014B97BC512293CA5C616`），收集 `2619` 项、
退出码 `0`，无失败标记。

Windows PowerShell 5.1 的完整流程冒烟写入
`artifacts/evaluation/terminal-continuation-v18/goal-acceptance-after-goal-exitcode/`，总快照
SHA-256 为 `C5B41616636DC9668314BCB934BBF0637F0E4E23C8CCE5110E4AFB24AE4D6851`，
`software_status=pass`、`hardware_status=offline_pending`、核心测试收集 `2619`、QGC custom
`66`；原始日志 SHA-256 为
`C56ECD6682D15E096BFA2E815F369ED523F0D6DB574445609EEEF713604FE002`。这只是宿主兼容性证据，
不关闭现场链路、REPLAY accuracy、HIL/REAL 或最终版本门。

## 现场 preflight wrapper 退出码健壮性（2026-09-04，`v0.3.0-pre.8`）

`scripts/run_field_link_preflight.ps1` 已将原生 Python 调用与
`prepare_field_link.ps1 -VerifyOnly` 的退出码读取改为防御式访问
`Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue`，未创建自动变量时使用零值。
这只修复 PowerShell `Set-StrictMode` 下的 wrapper 稳定性，不改变任何现场链路或 `-RequireReady`
门槛；失败探测仍写入 JSON，且 `hardware_link_ready=false`。

只读冒烟证据
`artifacts/evaluation/terminal-continuation-v18/field-link-wrapper-smoke-20260904b.json`
（7,861 bytes，SHA-256
`C437051B274C76ED4732FD0019FF20EC67FCCA16824900D9609193E1891CA0C`）以 1 个 watch 样本和
`192.168.1.0/30` 有界发现完成 wrapper/verifier；诊断模式退出码均为 `0`，但样本
`all_passed=false`、`hardware_link_ready=false`，发现结果为 SSH 1、MAVLink 0、RTSP 0。
该文件只记录当前台式机网络事实，不能替代 Jetson、GR01、相机、GNSS、HIL 或 REAL 验收。
对应脚本/wrapper/field-preflight 回归为 `51 passed`，日志
`artifacts/evaluation/terminal-continuation-v18/field-link-wrapper-regression-20260904.log`
（565 bytes，SHA-256
`5ED87FCDFDBE91521B726EDEBBC270047EA6F0DE4FA104749D72A3D0F5DEDBEF`）。随后完整 Python
回归为 `2616 passed / 1 skipped`（共收集 2617，退出码 `0`），日志
`artifacts/evaluation/terminal-continuation-v18/pytest-full-20260904-after-field-wrapper.log`
（3,481 bytes，SHA-256
`29AD92466E928C6DEC1CFBF1EC4D58C14AFF4190A74C11D1D2C976D372ABC5C0`）。

## 杆臂校正后的 parity 绑定状态（2026-09-04，`v0.3.0-pre.8`）

`GNSS→相机杆臂校正`已经进入 canonical `core_algorithm`，当前
`configs/release-parity.json` SHA-256 为
`BFE773CE40773A9AFA651E29FC2BA80FB1119B6B2519424A7A2A2348302BED24`，
`core_algorithm_hash=994ae1dffb3856eac5d71b479a7377f3f1b1c5beda2d154d6c2ee3b733c15af3`。
本轮全量 Python 回归为 `2616 passed / 1 skipped`（共收集 `2617`，退出码 `0`），日志
`artifacts/evaluation/terminal-continuation-v14/pytest-full-20260904-lever-arm-summary.log`
（3,536 bytes，SHA-256
`0194FF54B0A24456B1CF045698602C428F0D4FDE3DD4AB1AB7D9B63773329B1C`）。

旧六源 external SIM join
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-current-provider-fallback-parity.json`
仍绑定旧 parity `0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`，文件
SHA-256 为 `EF0631BF43B11DF6AC170C2F2A6A68C7CFF26AF800FD16260DC861C2EDF07557`。按当前 parity
执行 `scripts/verify_simulator_external_evidence.py --join` 后，诊断
`artifacts/evaluation/terminal-continuation-v15/external-join-revalidation-current-parity.json`
（625 bytes，SHA-256 `5F874F6BDE9FBD62F17226465D46C2242C786EC3020B0C342E29B2793430E283`）明确为
`gate_passed=false`，原因是 `join: stored summary does not match source recomputation`。
这一步是强制 fail-closed：旧 Docker/PX4/Gazebo 结果不能重标为包含杆臂校正的当前源码证据。

随后运行 `scripts/run_current_release_gate_pre8.ps1 -Strict`，wrapper 在写 manifest 前检测到
`simulator_external_evidence join parity hashes do not match the release-gate parity input`，
退出码 `1`；原始日志
`artifacts/evaluation/terminal-continuation-v15/e7-rebind-attempt-20260904T023000Z.log`
（776 bytes，SHA-256
`F3DF0617A033997169741B4F4BFBBBB9076FB042393BD7F556343210BC6D7721`）中没有生成新的
manifest/report。该次记录只反映当时的 PATH-only 探测结果：当时环境变量里没有 `docker.exe`，因此
该次 E7 续验没有启动 source。后续在本机 Docker Desktop 运行时目录发现 CLI 与已运行
daemon，并按当前 parity 完整重跑六个 runtime+AI source；新结果见下方 2026-09-04
六源重绑章节。v15 这次尝试仍保持 `gate_passed=false`，不能被回写成通过证据。

重开 E7 的顺序固定为：三机型各自以 `--execute --exercise-arm --exercise-target` 生成
runtime+AI source → 用当前 `configs/release-parity.json` 重新 join → 独立
`--join` revalidation → 再运行本 wrapper 的 report-only/strict。不要直接复制旧 join 或
手工改 parity 字段。

下方 2026-09-03 及更早的章节保留为历史 provenance；其中出现的 `0AFF...` parity、旧
external join 和旧 E7 文件名不代表当前源码状态，当前值以本节开头的 `BFE773...` 记录为准。

## 三机型确定性 SIM 场景矩阵（2026-09-04，当前 `v0.3.0-pre.8`）

在当前杆臂校正后的源码与 parity 下，运行
`scripts/run_simulator_scenario_matrix.py --scenario-directory configs/scenarios --frames 180
--dt 0.05 --require-fault-coverage --cover-faults`，覆盖固定翼、VTOL、多旋翼三个场景。
每个场景均完成 5,221 帧与 5,221 个 AI accepted steps；重复运行的记录哈希一致，GPS dropout/drift、
camera dropout、airspeed freeze、wind gust、battery drop 五类故障均被触发。证据
[`simulator-scenario-matrix-20260904-current-pre8.json`](../artifacts/evaluation/terminal-continuation-v16/simulator-scenario-matrix-20260904-current-pre8.json)
为 13,302 bytes，SHA-256
`6F39876CE26EFF7B8E4DCFF9E4E083E3A450441BA97F63439B62392BFB29A2FF`；独立
`verify_simulator_scenario_matrix.py` 退出码 `0`，`gate_passed=true`、
`deterministic_gate_passed=true`、`repeatability_checked=true`。这是 truth-free/in-process
确定性 SIM 子门；外部 3-D runtime、QGC 人工画面、真实硬件、室外 GNSS、实飞与 REAL 仍为
`not_claimed`，不能替代 Docker/PX4/Gazebo 或现场 E7 source。

## 外部 PX4/Gazebo + live-AI 六源重绑（2026-09-04，当前 `v0.3.0-pre.8`）

本轮通过直接调用本机 Docker Desktop CLI
`C:\Program Files\Docker\Docker\resources\bin\docker.exe`，固定镜像
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`，
为固定翼、VTOL、多旋翼分别执行 runtime 与 live-AI source。六个 source 的
`gate_passed`、`telemetry.qualified`、AI policy、target policy、容器清理均通过；每个
AI run 均使用 `--execute --exercise-arm --exercise-target`，记录 `ai_steps=2`、
`armed_observed=true` 与 `target_exercise=accepted`。证据目录为
`../artifacts/evaluation/terminal-continuation-v17/`：

| 机型 | runtime source SHA-256 | live-AI source SHA-256 |
| --- | --- | --- |
| fixed-wing | `7E301F994A24C68A097BA3289B23BE5609E8564B940E25D22588409C72F74B35` | `619244E2AD12093B8E7AECCC485BD5EFDB788826225798E212AB18114F13A0AC` |
| VTOL | `062F7D96AD9E47580CEC2BA9784B6A8C6328CE9D1AF679D8FF48253F75248A99` | `8ABFE4B5A7CA3EB83E5D34153CAAB349679698C48EA427BBC1C419002FD2D342` |
| multirotor | `34E46C7699D22337E005E551B62F9674E803F5681525BF3FB79970D2E31B7E84` | `93C481CB6C4C39FDA1F33830BFFA89B0BE6D4C965790BAB5607797693EB16EFE` |

六源 join 为
[`simulator-external-runtime-ai-evidence-20260904-current-pre8.json`](../artifacts/evaluation/terminal-continuation-v17/simulator-external-runtime-ai-evidence-20260904-current-pre8.json)，
5,287 bytes，SHA-256
`E0B2BA71345D6C046CA9EEFAA42DA45325B8712069CB2000DDAEEF3E7B3D858C`；独立
`--join` revalidation 为
[`external-join-revalidation-current-parity.json`](../artifacts/evaluation/terminal-continuation-v17/external-join-revalidation-current-parity.json)，
565 bytes，SHA-256
`0BEF11653C13933E10F27E6A63F0DB3A1E89DCBC4438F53FE56CEF1EE58FD688`。两者均为
`gate_passed=true`、`reasons=[]`，并确认 source 重算与当前
`configs/release-parity.json` 一致。

该 join 已绑定到
`artifacts/evaluation/terminal-continuation-v17/e7-rebind/` 的当前 E7 report-only/strict
输入：manifest 为 `release-gate-pre.8-current-20260904T030000Z-v6-bound.json`，
report-only 为 `release-gate-pre.8-current-20260904T030000Z-v6-bound-report.json`，strict
report 为 `release-gate-pre.8-current-20260904T030000Z-v6-bound-strict.json`。manifest SHA-256 为
`0558A36936E82575819711351C1C9012A69607CAD0F0FF1FDC470F571C96A51E`，report-only 为
`AEA351DF5C7E74999607AF96E7C1918455D82B5CDE187C0EBAE5C952D91E66ED`，strict report 为
`B0757737453520E309101259E02192F192042CF33F5A47BF8F83287C6E4888C8`。E7 已识别并接受
该 external join，但总门仍为 `gate_passed=false` / `release_allowed=false`（31 个阻断项），
包括 REPLAY accuracy、真实现场链路、HIL/REAL、人工 Fly View 验收、各模式 Mission-path
验收与 prerelease channel。该证据关闭的是 software-only external PX4/Gazebo/live-AI
子门，不代表真实 Pixhawk/Jetson/相机/室外 GNSS、实飞或最终 `v0.3.0` 已通过。

随后将同一现场预检显式绑定到新的
`artifacts/evaluation/terminal-continuation-v17/e7-rebind-fieldlink/`，未覆盖前一份 E7
证据。该目录的 manifest 为
`release-gate-pre.8-current-20260903T184551Z-v6-bound.json`（17,774 bytes，SHA-256
`E8841054FB71BD5FC0374ED6C12A5E3485444C161869AE627C29673CC1506A0C`），report-only 为
`release-gate-pre.8-current-20260903T184551Z-v6-bound-report.json`（46,662 bytes，SHA-256
`9E40DC8A46E7235504A244D8B00697B0D0D2C13AD5A6E986EDC3ECFB19F1B3D9`），strict report 为
`release-gate-pre.8-current-20260903T184551Z-v6-bound-strict.json`（46,662 bytes，SHA-256
`0840AD72023A877BB258B2A7975ACC8002DF1A5C0F89502CE972A307961EC92D`）。wrapper 严格退出码
为 `2`，仍保留 31 个阻断项；其中 `real_field_link` 明确绑定当前
`hardware_link_ready=false`，没有把网络不可达误判成现场通过。

现场链路只读预检另存为
`../artifacts/evaluation/terminal-continuation-v17/field-link-preflight-current-20260904.json`
（3,434 bytes，SHA-256
`C64890B38EC040FD365C8AB9D6BA3D31620F7442F256A7BF812CBE803009039F`）。Jetson SSH
`192.168.144.20:22`、GR01 MAVLink `192.168.144.11:5760`、相机 RTSP
`192.168.144.108:554` 均为 timeout；本机 `14580/14581` UDP bind 成功，故当前
`hardware_link_ready=false`。该证据是只读可达性检查，不关闭现场、HIL 或 REAL 门。

另做了有界只读发现以排除台式机迁移后的地址变化：
`../artifacts/evaluation/terminal-continuation-v18/field-link-discovery-192.168.1-20260904.json`
（8,525 bytes，SHA-256
`79D3AA8F6E873D7C682DF3C719E941F25E5172EBEBC517BA01410651B0B89A92`）扫描 254 个地址，
5 个 SSH 响应，MAVLink `5760` 与 RTSP `554` 均 0 命中。发现结果只做诊断，不自动选择端点、
不登录未知主机，也不关闭现场/HIL/REAL 门。

在上述绑定之后运行了完整 goal-acceptance，输出目录为
`../artifacts/evaluation/terminal-continuation-v18/`；主快照
[`multidetect-goal-acceptance-20260904-current-pre8.json`](../artifacts/evaluation/terminal-continuation-v18/multidetect-goal-acceptance-20260904-current-pre8.json)
为 60,087 bytes，SHA-256
`CB6A884DFA76E52DCB8EB34F8EF678C58EA137361C31D08FDF01F3D991306507`，脚本退出码 `0`。
其中核心测试收集 `2617`、QGC 自定义测试 `66`、QGC protocol self-test/QML parse 通过，
六源 external gate 已绑定且通过；`hardware_status=offline_pending`、REPLAY readiness 和
现场链路仍未通过。剩余六项 live gate 是 deployment replay accuracy、现场标定与舵面安全、
真实 DET→TRK→LCK→TGT、Jetson soak，以及三种外部 PX4/Gazebo 场景的人工 Fly View 验收。

## E7 wrapper 健壮性修复与当前绑定刷新（2026-09-04，`v0.3.0-pre.8`）

`scripts/run_current_release_gate_pre8.ps1` 的 `Invoke-Python` 已针对严格模式补上
`LASTEXITCODE` 缺失处理：调用解释器后用 `Get-Variable -Name LASTEXITCODE` 读取原生退出状态，
没有该自动变量时采用确定性 `0`，避免 wrapper 在 manifest 生成之前异常退出。这个改动不改变
任何 release-gate 条件；`test_current_release_gate_wrapper.py` 与 goal-acceptance 接线回归为
`15 passed`，包含 product-version、parity、external-binding 的完整 E7 专项集合为 `56 passed`。
原始日志 `../artifacts/evaluation/terminal-continuation-v18/e7-wrapper-regression-20260904.log`
为 618 bytes，SHA-256 `762CEA25A1B6F793DECC8B945CEF2FE81D7EB780B44CD689FB57CE3FAA594C12`。

使用当前 parity 的六源 external join、v18 field-link preflight、replay-set、三机型 3-D control、
SIM/actuator safety 与 PX4 Mission transaction evidence 后，严格 wrapper 在
`../artifacts/evaluation/terminal-continuation-v18/e7-rebind-current-v5/` 写入新的绑定文件：

| 输出 | 大小 | SHA-256 | 结果 |
|---|---:|---|---|
| `release-gate-pre.8-current-20260903T190334Z-v6-bound.json` | 17,702 bytes | `1C88F8C7EC0F087126C3B274C17CFF23412969C213A158BA261DB6B8EB498CC8` | manifest，external join 已接受 |
| `release-gate-pre.8-current-20260903T190334Z-v6-bound-report.json` | 46,713 bytes | `04D04E9B822935DB0150F7AC0CE93BAE209E5C7D0674B24244B6E173DA9ECDFA` | `gate_passed=false`，31 blockers |
| `release-gate-pre.8-current-20260903T190334Z-v6-bound-strict.json` | 46,713 bytes | `2F9A7CD22BEA34C12832316759A8147BAC08E7F5E9F6415B4DF99A4ED0C394EF` | strict evaluator exit `2` |

报告中的 `simulator_external_evidence.passed=true`、source hash 和独立 revalidation 均通过；
`real_field_link.hardware_link_ready=false`，REPLAY accuracy/deployment、现场硬件、人工 Fly View、
HIL/REAL、prerelease channel 等阻断保持不变。该刷新只更新 evidence provenance，不能把
software-only SIM/Docker 结果升级为真实飞行或最终 `v0.3.0` 发布。

## 外部 SIM 运行时契约与 Docker AI 续验（2026-09-04，`v0.3.0-pre.8`）

`runtime-preflight` 已扩展为可选的场景资产绑定预检。传入
`--repository-root`、`--scenario`、`--px4-workdir` 时，它只读解析仓库自有世界/模型资产，
记录文件 SHA-256、stack-plan SHA-256 和 PX4 `etc/init.d-posix/rcS` 启动锚点，并输出
`asset_preflight`、`px4_workdir_preflight`、`external_runtime` 三个节点。预检不会启动进程、
容器或打开 UDP；因此 `external_runtime.gate_passed=false`、
`external_runtime.external_runtime_gate_passed=false` 是强制边界，`ready_to_attempt=true`
只表示候选工具链和前置资产已经满足尝试条件。确定性 flight-model 回退明确标为
`automatic=false`、`used=false`，必须由上层编排器显式记录，不能静默切换。

本轮场景资产预检证据为
[`runtime-preflight-scenario.json`](../artifacts/evaluation/terminal-continuation-v10/runtime-preflight-scenario.json)，
7,188 bytes，SHA-256
`1E243FBA41273CA94FBB6B349573FB4206B4A26CEED5C55778CF545A3A2590E1`；当前 Docker
client/daemon 候选就绪，但该文件仍是无启动的只读预检。

随后运行固定 digest 的 `px4io/px4-sitl-gazebo` Docker/SITL AI 闭环，证据为
[`simulator-docker-ai-acceptance-external.json`](../artifacts/evaluation/terminal-continuation-v10/simulator-docker-ai-acceptance-external.json)，
114,687 bytes，SHA-256
`DF58B68B40D34683DFE1309A4A45965FCD26EC40887B7077DBBA6BB65752C1FF`。该证据的
`gate_passed`、`preflight_gate_passed`、`runtime_gate_passed`、`telemetry.qualified`、
`ai_policy_gate_passed`、`target_policy_gate_passed` 均为真，`ai_policy_steps=2`，容器
实际启动、产生 PX4 telemetry 与共享 MANUAL_CONTROL AI 输出，并在结束后
`cleanup.removed=true`。这是 software-only Docker/SITL 子门，QGC Fly View 人工视觉、
真实 Pixhawk/Jetson/相机、室外 GNSS、执行器、HIL/REAL 和最终 `v0.3.0` 发布门仍保持
`not_claimed`/blocked。

## 外部 PX4/Gazebo + 原生 QGC + VTOL Mission 组合续验（2026-09-04 本地，2026-09-03 UTC，`v0.3.0-pre.8`）

300 帧短跑的失败快照保留在
[`combined-sim-qgc-20260903T164006Z-39412/combined-evidence.json`](../artifacts/evaluation/combined-sim-qgc-20260903T164006Z-39412/combined-evidence.json)：
QGC type-24 sequence 在 `300` 处结束，未满足 Mission-ready 后额外帧屏障，因此严格失败，不能
拿来当通过证据。按脚本注释把帧预算提升到 900 后，成功 source 为
[`combined-sim-qgc-20260903T164443Z-4900/combined-evidence.json`](../artifacts/evaluation/combined-sim-qgc-20260903T164443Z-4900/combined-evidence.json)，
249,113 bytes，SHA-256
`0DC4044DB459888D4DC1648CC8266B545FBEF86D346FF45E8199C75909911762`；独立 verifier
[`combined-evidence-verification.json`](../artifacts/evaluation/combined-sim-qgc-20260903T164443Z-4900/combined-evidence-verification.json)
为 1,956 bytes，SHA-256
`A31188E69D6EAA5A8752CA11CEF06D2CFE63AAB8601A3ABCEC2F980B536CF0CF`，40/40 checks 通过，
verifier 退出码 `0`。

为避免绑定旧 run 的 envelope，随后为该 source 单独生成
[`release-mode-sim-combined-qgc-20260903T164443Z-4900.json`](../artifacts/evaluation/terminal-continuation-v10/release-mode-sim-combined-qgc-20260903T164443Z-4900.json)
（542 bytes，SHA-256
`80AB0572968BE5992E523BDD8ED2E402D996FAA772A9ED70AB0974769FF1EB52`），再用
`verify_combined_sim_evidence.py --require-release-mode` 复核；输出
`combined-evidence-verification-release-mode.json`（2,596 bytes，SHA-256
`BDD55C221332DDB27599433AF09B58F078C26CC95399EA88B97D84EC5D1489F9`），40/40 checks 仍通过。

为将该通过的 SIM 子门纳入当前 prerelease 账本，使用同一份其他模式输入生成了独立 E7
绑定目录 `../artifacts/evaluation/terminal-continuation-v10/e7-combined-qgc-rerun/`：manifest
（17,864 bytes，SHA-256
`D2A4FE349CB45D0AC872ACA589532B6A3E2586EB1196856791647BE708FA9207`）、report-only
（47,873 bytes，SHA-256
`DA99390FE22DFFD8D3280DCAA94B3A5B89E5755BAB23DA2C0A661E14A4D71B18`）与 strict report
（47,873 bytes，SHA-256
`D71E6AB91BEEE570A5F321ED3A9309D9AADCE9433B197D2E0CE9CC03A023E3D5`）。E7 中
`simulator_combined.passed=true`、source/envelope 路径和 SHA-256 完整匹配；总门仍因
REPLAY accuracy、现场 `hardware_link_ready=false`、人工视觉、HIL/REAL 与 prerelease channel
保持 `gate_passed=false`/`release_allowed=false`，strict 退出码 `2`。

通过项包括：固定 digest 的 `px4io/px4-sitl-gazebo` 容器真实启动/清理；camera、CameraInfo、
NavSat 必需 publisher 稳定检查 `2/2`；9 个动态 loopback 端口互异、保护 `14550` 未被使用且
全部释放；同一 QGC 原生 Fly View 上 RTSP、signed type-23、native type-24 的计数均为 `900`。
Mission-ready 的 QGC type-24 序列为 `569`，目标 `574`，捕获后 `579`；VTOL Mission
`gate_passed=true`，7 项 INT-only Mission 全部 readback/reached，观察到 `MC→FW→MC`、
AUTO.MISSION、land/disarm，最大距离 `231.25 m`、最大相对高度 `23.783 m`。QGC 原生
QQuickItem 像素/传输绑定机器门通过，但 `operator_visual_acceptance=not_claimed`。

该 source 只关闭 software-only 外部 SIM/QGC/Mission machine 子门；真实 Pixhawk、Jetson、
相机、室外 GNSS、执行器、HIL/REAL、人工 QGC 画面和最终 `v0.3.0` 发布门仍保持阻断。
重跑日志为
[`combined-qgc-run-rerun-900.log`](../artifacts/evaluation/terminal-continuation-v10/combined-qgc-run-rerun-900.log)，
249,395 bytes，SHA-256
`32F4BFE2E1A9D90BB231514C678DB2FCB3F17EBA4B13211A4F0A45B32A0EC6AE`。

## 当前 terminal continuation v8 绑定刷新（2026-09-04 本地，2026-09-03 UTC，`v0.3.0-pre.8`）

在不覆盖 `latest` 或历史证据的前提下，使用
`scripts/run_current_release_gate_pre8.ps1` 的当前默认输入，在
`artifacts/evaluation/terminal-continuation-v8/` 生成了新的 E7 report-only/strict 绑定：

| 文件 | 大小 | SHA-256 | 状态 |
|---|---:|---|---|
| [`release-gate-pre.8-current-20260903T161008Z-v6-bound.json`](../artifacts/evaluation/terminal-continuation-v8/release-gate-pre.8-current-20260903T161008Z-v6-bound.json) | 17,786 bytes | `9A4F03F02A49A69DA47DC6C0BE7B15A8849FA9DE230A05A1B37B70207C3C83B9` | `v0.3.0-pre.8` manifest |
| [`release-gate-pre.8-current-20260903T161008Z-v6-bound-report.json`](../artifacts/evaluation/terminal-continuation-v8/release-gate-pre.8-current-20260903T161008Z-v6-bound-report.json) | 47,742 bytes | `C3A86200F591941473CF8B684C128AAC1B7FE837F3A8BA2882ED03E2BB40173D` | `gate_passed=false`, `release_allowed=false` |
| [`release-gate-pre.8-current-20260903T161008Z-v6-bound-strict.json`](../artifacts/evaluation/terminal-continuation-v8/release-gate-pre.8-current-20260903T161008Z-v6-bound-strict.json) | 47,742 bytes | `FC498FF3ED174E0F740409AC988139E2664D694875247C900E8B835EFDEB9382` | strict verifier exit `2` |

严格复核仍列出 31 个阻断项。三机型 `simulator_3d_control` 的
`gate_passed`、`deterministic_gate_passed`、`records_hash_bound` 与 S4 safety 子门保持为真；
阻断项仍是 synthetic/未复核的 REPLAY accuracy、现场 `hardware_link_ready=false`、三模式
人工 Fly View/Mission、HIL/REAL 硬件与真实控制，以及 `prerelease` 通道。该次刷新只更新
软件 provenance，不能升级为真实 PX4/Gazebo、室外 GNSS、真实设备或最终 `v0.3.0` 发布证据。

## 正例 replay readiness 续验（2026-09-04，terminal continuation v7，pre.8）

对独立 synthetic positive evaluator fixture 运行
`scripts/check_replay_set_readiness.py`（携带 hash-bound `accuracy-report.json`），输出
[`terminal-continuation-v7-replay-readiness.json`](../artifacts/evaluation/terminal-continuation-v7-replay-readiness.json)
为 11,263 bytes，SHA-256
`E3FA53E95FA566BC9816FD4D9CFFC3902949B3D5176BC974E1A5F0ADFBA50EFC`。fixture manifest 为
4,982 bytes（SHA-256 `854BE67B34940C9937A61AE8EC9AA19DA56B0BEC11E536EB5DE2EA8AF217A32E`），
accuracy report 为 12,367 bytes（SHA-256
`2023CA574B3384080FE7D61C7572335D896EB5F7F40EAC7AFEF7E3E356789972`）。

readiness 确认六层正例真值/预测覆盖、manifest/canonical hash binding 和 evaluator 重算均为
真；但六个条目的 `source_kind` 仍为 `synthetic_demo`、`annotations_reviewed=false`，所以
`manifest_gate_passed=false`、`accuracy_gate_passed=false`、总 `gate_passed=false`，并保留 20
个 `blocking_reasons`。这份快照只验证 evaluator 接线和 fail-closed 门逻辑；下一步必须换成
真实部署录像、独立复核标注和真实模型输出，不能将它写成部署域精度通过。

## 台式机现场链路只读续检（2026-09-04，terminal continuation v9，pre.8）

迁移后的台式机使用
`scripts/run_field_link_preflight.ps1 -ProtocolChecks -RequireRoute -RequireReady` 做三样本
只读检查，报告
[`field-link-preflight-20260903T161925Z.json`](../artifacts/evaluation/terminal-continuation-v9/field-link-preflight-20260903T161925Z.json)
为 16,804 bytes，SHA-256
`15AA28819D7B1D94AE082C4EE3EA458D64A4BCBFC2CF05F299EEED07586B992E`；wrapper 日志为
18,593 bytes，SHA-256
`0E33C3ED1C14E2071F68D7BC9AD7D67C88780ABD4A6F03020337644977836914`。

三个样本均显示 Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760`、相机
`192.168.144.108:554` TCP/协议超时；路由探测使用 `192.168.1.13`，不在期望
`192.168.144.0/24`，所以 `route_checks_passed=false`、`hardware_link_ready=false`，
wrapper/verifier 均退出 `1`。本地 `14580/14581` UDP 端口可绑定、配置校验通过；本次只
刷新网络事实，不登录或发送 MAVLink/RTSP 控制数据。接通飞机 Ethernet 后必须用实际地址
重跑预检，再进入 GNSS、RANGE_STATUS、相机和执行器验收；该快照不构成 REAL/HIL 通过。

## 当前 terminal continuation v6 绑定（2026-09-03，`v0.3.0-pre.8`）

使用 `scripts/run_current_release_gate_pre8.ps1` 的当前默认输入，并显式替换为 terminal
continuation v5 的三机型 3-D control 与 S4 safety 证据，在
`artifacts/evaluation/terminal-continuation-v6/` 独立生成了一份 report-only E7 清单和
报告。不会覆盖 `latest` 或历史产物：

| 文件 | 大小 | SHA-256 | 聚合状态 |
|---|---:|---|---|
| [`release-gate-pre.8-current-20260903T160000Z-v6-bound.json`](../artifacts/evaluation/terminal-continuation-v6/release-gate-pre.8-current-20260903T160000Z-v6-bound.json) | 17,839 bytes | `8D86A97CD4C8CB099CB852EA11FE3C325AB783936A00F1E5EDC77EEE58DCC619` | pre.8 manifest |
| [`release-gate-pre.8-current-20260903T160000Z-v6-bound-report.json`](../artifacts/evaluation/terminal-continuation-v6/release-gate-pre.8-current-20260903T160000Z-v6-bound-report.json) | 47,799 bytes | `86EC0A976FB5AD1D4968805BF21AE270A561F77017D25BAF0B22CDD674ADD572` | `gate_passed=false`, `release_allowed=false` |

清单顶层 `simulator_3d_control` 现在包含三份 hash-bound record：fixed-wing
`8731052699FDD206B72756B601EC150B8EBD4C4B9AD9405483DAF89FD218A60`、multirotor
`C24BE98F5C14632DF6818E4A1E0B1EE3571A9CA5417C15AF007CA877939A0F6E`、VTOL
`11E0D790D7697E2F557C7861CC86CEEC661213611957FF456E7473A2AC1DEE4A`；三份记录的
`gate_passed`、`deterministic_gate_passed`、场景/模式唯一性和 `records_hash_bound` 均为真。
同一清单还绑定 S4 safety 报告
`7293321BF87CD10E15199847BB9AEC5CCBFBE083FDD2DC0665F8071E95FC38F6`，其取消隔离、
非法 GPS fail-closed、failsafe 后控制阻断和终态 Mission 拒绝检查通过。

E7 聚合器仍按发布门原语义保留以下阻断：部署域 REPLAY/accuracy 与覆盖复核不完整；现场
`hardware_link_ready=false`；REPLAY/SIM/HIL/REAL 的人工 Fly View 和 Mission 尚为
`not_claimed`；HIL/REAL 的硬件与真实控制尚未声明；产品通道仍是 `prerelease`，最终版本
面也未切换到 `v0.3.0`。wrapper 的 report-only 退出码为 `0`，只表示阻断状态已成功
写入报告；严格模式仍应以非零退出表示不可发布。外部 PX4/Gazebo 运行、视觉和硬件字段
继续保持 `false/not_claimed`，因此该绑定只收口 software/SIM contract，不升级为 HIL、
REAL、室外 GNSS 或最终版本证据。随后直接对同一清单运行严格
`scripts/check_release_gate.py`，退出码为 `2`，与 report-only 报告的 fail-closed 状态一致。

## 开发环境依赖闭包（2026-09-03，pre.8）

项目 `[dev]` extra 已显式加入 `jsonschema>=4.20`，并同步到 `uv.lock`；项目 venv 的
`uv lock --check` 通过。这样 RTSP schema 自检和其它 JSON Schema 测试在 clean checkout
不会再因 venv 缺少 `jsonschema` 而在 collection 阶段中断。新增的 replay-set 校准绑定回归
还确认有效 verification 不能移植到另一份 replay manifest；项目 venv 全量 pytest 共收集
`2606` 项，`2605 passed / 1 skipped`，日志
`artifacts/evaluation/pytest-full-20260903-dev-dependency-v1.log`（2,997 bytes，SHA-256
`B4F17F1B835467D0338B82EC7FBF825B756D4F611163A2CEFAE703001994CC91`）。
同时以 `pip install -e ".[dev]"` 重建了项目 editable metadata，消除迁移后残留的
`multi-detect 0.1.0`；pip metadata、`multidetect.__version__` 与产品清单现在一致为
`0.3.0rc8 / v0.3.0-pre.8`，`pip check` 通过。

## 终端 continuation 验收快照（2026-09-03，pre.8）

为避免覆盖历史 `latest` 文件，随后把完整验收输出写入独立目录
`artifacts/evaluation/terminal-continuation-v1/`。快照
`multidetect-goal-acceptance-20260903-terminal-continuation-v1.json` 为 44,931 bytes，
SHA-256 为 `9E207A3B7E6C03BF69C9EB28E3568F56438EA9ACB8C66C60CC1E5FCCD46065DD`；版本仍是
`0.3.0-pre.8`，核心 pytest 收集 `2606` 项，QGC 自定义测试 `66` 项，software 子门和
三机型 SIM/PX4 Mission 事务均通过。快照同时重新记录台式机现场状态：`Ethernet`
`192.168.1.13/24`，无 `192.168.144.0/24` 匹配接口，Jetson 与相机不可达，
`hardware_status=offline_pending`。因此这轮只刷新可审计的软件状态，现场 GNSS、真实
Pixhawk/Jetson、部署域 replay、HIL/REAL 和人工 Fly View 仍不得标记为通过。

同一 canonical portable 入口随后执行无界面启动烟测：
`artifacts/evaluation/terminal-continuation-v1/qgc-simple-boot-20260903T135047Z.log`
（220 bytes，SHA-256 `83437047EDDDE232A7E6F6AA3C7727CB0B6C6555D62ED0C19FA84D62E3F135D9`），
退出码为 `0`。日志只包含 Qt Multimedia FFmpeg 版本和点尺寸提示，没有 QML 初始化失败、
断言或崩溃；这仍是 headless smoke，不等价于人工 Fly View 验收。

22:00 后对同一窗口做了句柄级 `PrintWindow` 观察，截图
`artifacts/evaluation/terminal-continuation-v1/qgc-flyview-window-20260903T141750Z.png`
（1,163,366 bytes，SHA-256 `5645F55713060EB8D8C2D51F97D73F609DE5B08DFCB19FBE60193827CFE772E6`），
元数据为
`artifacts/evaluation/terminal-continuation-v1/qgc-flyview-capture-20260903T141750Z.json`。
窗口显示原生 QGC Fly View、`M1·DET`、视频/地图表面和“等待 Jetson 深度网格”的断开等待态；
这是工程观察，不是操作员视觉签字，`operator_visual_acceptance` 继续为 `not_claimed`。

## 当前 parity external SIM 续验与 goal acceptance v2（2026-09-03，pre.8）

为保证外部 SIM 证据没有沿用旧 parity，使用当前
`configs/release-parity.json`（SHA-256
`0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`、
`core_algorithm_hash=434b86c6a8b677e60a4ae5766586b603855609e43cc0521695aa1e21995963f9`）重新
运行 `scripts/verify_simulator_external_evidence.py`。输入 join
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-current-provider-fallback-parity.json`
的 SHA-256 为 `EF0631BF43B11DF6AC170C2F2A6A68C7CFF26AF800FD16260DC861C2EDF07557`；verifier
退出码 `0`，`source_recomputed=true`、`gate_passed=true`、`release_label=0.3.0-pre.8`。

本轮新写 revalidation
`artifacts/evaluation/terminal-continuation-v2/simulator-external-runtime-ai-revalidation-current-provider-fallback-parity.json`
（558 bytes，SHA-256
`5576A33BE03572344825430CE78DDF4B2F7FB03087F1DE025DAFC8325E9E6D35`），并把输入和 revalidation
一起传给 `scripts/run_goal_acceptance.ps1 -RequireSimulatorExternalEvidence`。完整快照
`artifacts/evaluation/terminal-continuation-v2/multidetect-goal-acceptance-20260903-terminal-continuation-v2.json`
为 45,325 bytes，SHA-256
`B4B3E9C3F2566FAF6313FE7D42CD7E8706C8FAEB3AB3B4E715491287D8491589`；核心 pytest `2606`、
QGC custom `66`、`software_status=pass`，顶层 external binding 的 `provided`、`verified`、
`source_recomputed` 和 `gate_passed` 均为 `true`。

因此 goal acceptance 不再把“运行外部 PX4/Gazebo”重复列为未完成项，而只保留“三机型人工
QGC Fly View 视觉验收”这一外部 SIM 后置门。六项剩余 live gates 为部署域六分层 replay/accuracy、
20+ 视角 ChArUco 合格标定、断推进器执行器方向/限位/取消/失锁回退、真实检测状态链、Jetson
soak，以及人工 Fly View。external runtime 证据仍是 truth-free/software-only，无物理控制输出；
`hardware_status=offline_pending`、`operator_visual_acceptance=not_claimed`，总门和最终
`v0.3.0` 发布状态保持关闭。

## 台式机现场链路续检 v3（2026-09-03，pre.8）

同一台式机再次运行只读 `scripts/run_field_link_preflight.ps1`，配置仍为
`192.168.144.20:22`（Jetson）、`192.168.144.11:5760`（GR01）、`192.168.144.108:554`（RTSP），
期望源网段 `192.168.144.0/24`。报告
`artifacts/evaluation/terminal-continuation-v3/field-link-preflight-20260903-terminal-continuation-v3.json`
为 6,686 bytes，SHA-256 为
`CD8D3819670444803DD793E1B7F13258E5101CCA8E7CFD71F8428D7BBC6B665D`。结果是
`hardware_link_ready=false`、wrapper/verifier 退出码均为 `1`：`Ethernet=192.168.1.13/24`，
三个现场 TCP/协议探测均超时，路由探测选择的源地址也仍在目标网段之外；本地 UDP `14580/14581`
可绑定。该报告是 read-only 现场事实，不包含登录、MAVLink/RTSP 会话或控制输出，因此只保持
`hardware_status=offline_pending`，不改变 v2 software acceptance、REAL/HIL 或最终发布门。

该现场报告随后绑定到新的完整 acceptance
`artifacts/evaluation/terminal-continuation-v3/multidetect-goal-acceptance-20260903-terminal-continuation-v3.json`
（45,914 bytes，SHA-256
`A112A891F52B137EF293673DF7A4F1CC7FDC4F6DFE2DA9B09042D8737F8BA8C8`）。该快照重新收集核心
pytest `2606` 和 QGC custom `66`，`software_status=pass`；field binding
`provided=true`、`verified=true`，但 `hardware_link_ready=false`、verification exit `1`，
所以 `hardware_status=offline_pending`，六项 live gates 仍保持未完成。

作为离线 SIM 的额外压力检查，三机型矩阵又以 `--cover-faults --require-fault-coverage`
运行到所有声明故障都实际触发，并保留 5 秒尾部窗口。报告
`artifacts/evaluation/terminal-continuation-v4/simulator-scenario-matrix-fault-covered-pre8.json`
为 13,298 bytes，SHA-256 为
`7AB2CB409B27FB9AB94B95CD9F450700CA79B9814C827B66DD56F7A70AABCAF3`；自动扩展到 2,651 帧，
fixed-wing/VTOL/multirotor 的 fault coverage ratio 均为 `1.0`，重复运行、deterministic gate
和 verifier 都通过。该结果仍标记 truth-free/software-only，不替代外部 PX4/Gazebo、真实硬件、
室外 GNSS、HIL/REAL 或人工 Fly View 验收。

在同一版本和同一 parity 下又重新执行了共享 3-D control 与 S4 safety 子门。控制报告
`artifacts/evaluation/terminal-continuation-v5/simulator-3d-control-pre8.json` 为 83,252 bytes，
SHA-256 为 `493AE72466F1BDEC1F5C8AD225B41B86D1B4A94A5E4DC1AD0F7791E77059C7BD`，其
`gate_passed=true`、`deterministic_gate_passed=true`；manual QGC、AI authority、人工接管抑制/恢复、
watchdog neutral/failsafe、native QGC-only、Mission/flight write block 和 loopback isolation 均通过。
由于桌面当前没有 PX4/Gazebo 可执行文件，`external_runtime_gate_passed=false`，视觉/硬件仍为
`not_claimed`。S4 报告
`artifacts/evaluation/terminal-continuation-v5/simulator-safety-pre8.json` 为 6,991 bytes，
SHA-256 为 `7293321BF87CD10E15199847BB9AEC5CCBFBE083FDD2DC0665F8071E95FC38F6`，
`gate_passed=true`、`truth_free=true`；取消隔离、非法 GPS fail-closed、failsafe 后无控制和终态
Mission event 拒绝均通过。两份结果只关闭 software/SIM 子门，不替代外部 PX4/Gazebo、真实硬件、
室外 GNSS、HIL/REAL 或人工 Fly View 验收。

为把上述控制契约扩展到全部三种机型，分别以 fixed-wing、VTOL、multirotor scenario manifest
重新生成 control evidence，并调用 `verify_simulator_3d_control_evidence.py --bind` 做独立
hash-bound binding。绑定报告
`artifacts/evaluation/terminal-continuation-v5/simulator-3d-control-binding-pre8.json` 为
2,380 bytes，SHA-256 为
`6C5F4DD6F65490A13ACDC3B19887233EFFDC1FD27F5ACD6611A6AD85B1CA73BF`。其 verifier 确认
`all_airframes`、`all_gate_passed`、`all_deterministic_gate_passed`、`records_hash_bound`、
`unique_modes`、`unique_scenarios` 全为 `true`；三份 record 都保留
`external_runtime_gate_passed=false`，因此只关闭 software/SIM control contract 子门，
不替代外部 PX4/Gazebo、真实硬件或人工 Fly View 验收。
同一 verifier 输入连续运行两次，binding 输出字节完全一致，两个输出的 SHA-256 均为
`6C5F4DD6F65490A13ACDC3B19887233EFFDC1FD27F5ACD6611A6AD85B1CA73BF`。

## 当前 pre.8 便携路径绑定（2026-09-03）

SIM evidence 的本地路径已从 canonical 内容中解耦：runner 的 `world_origin.world_path`、
矩阵的 `manifest` 与 `scenario_directory` 均使用仓库根相对 POSIX 路径，绝对路径只用于实际
文件访问。等价 checkout 回归通过后，当前矩阵
`artifacts/evaluation/simulator-scenario-matrix-20260903-portable-paths-pre8.json` 的文件
SHA-256 为 `06C8E829CCA2321C5D10F2F700E04D2469772E0C9C6E7711738B160DBC110FC1`（13,512 bytes），
嵌入 canonical SHA-256 为 `CE87333B43B3D6E5964C031E27AE57EA3F682787EB54FE2F202DCD5FAD1FF95E`。
独立 verifier 退出码为 `0`，三机型故障覆盖与重复运行均通过。

该矩阵随后又与当前 parity 和六源 external SIM join 一起显式绑定到
`artifacts/evaluation/release-gate-pre.8-current-20260903T084500Z-v6-bound.json`（17,723 bytes，
SHA-256 `818A77FAB6F7C5AC6735726438CB16E447D16A7058A7C17E7611E8CC4438ED9D`）及最新
report-only 复核报告（47,716 bytes，SHA-256
`BD79B0B83FB46C2A531327B273AC5E40A8566FBF52A9F0D49780D497FEA4FF64`）。E7 读取时已重新计算
矩阵文件 hash 与 canonical digest，矩阵子门为通过；总门仍保持
`gate_passed=false` / `release_allowed=false`，不改变 REPLAY、现场硬件、人工 Fly View、
HIL/REAL 和最终 `v0.3.0` 的 pending 状态。

## REPLAY / 现场证据严格解析（2026-09-03，当前 pre.8）

发布门读取 replay-set manifest、独立 accuracy report 和参与 IoU 的 truth/prediction JSONL 时，
会拒绝重复 JSON key、`NaN/Infinity`；manifest、recording、truth、prediction 和 accuracy
report 路径链上的符号链接也会被拒绝，UTF-8 BOM 继续兼容。后出现的字段不能覆盖部署来源、
正例覆盖、阈值或 `gate_passed`，并且 accuracy report
仍需经过 manifest/canonical SHA-256 绑定和 fresh evaluator recomputation。发布门通用模式证据、
校准、release-mode envelope、SIM combined/external、安全证据、场景矩阵和 3-D control 读取也
复用严格 JSON 解析；相机标定文档、室外深度标定文档和 JSON 样本行也通过同一入口，新增
回归后全量 Python pytest 共收集 `2605` 项，`2604 passed / 1 skipped`（Windows 账户缺少
symlink 特权的路径回归自动跳过），ruff/compileall/schema 校验通过。完整运行日志为
`artifacts/evaluation/pytest-full-20260903-rtsp-multiclient-v4.log`（3,481 bytes，
SHA-256 `1CF6757BB855F7D0F51B311D2BA7E528D210550A26EC73399A1B298A68028B69`）。当前 parity
文件 `configs/release-parity.json` SHA-256 为
`0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`，
`core_algorithm_hash=434b86c6a8b677e60a4ae5766586b603855609e43cc0521695aa1e21995963f9`。

ONNX Runtime provider 统一按 `TensorRT → CUDA → CPU` 尝试。默认策略只有在 session 构造
失败时按后缀回退，显式 provider 列表保持严格失败；`model-check` 与 `jetson-bench` 会
记录可用 provider、尝试序列、异常摘要和最终选择，现场 Orin/JetPack 仍需单独验证。

## RTSP 多客户端软件子门（pre.8，2026-09-03）

`MultiDetectReplayRtspPublisher` 的默认单客户端路径使用非共享 media factory，按 appsrc
generation 做真实扇出；支持 `--minimum-client-count 1..64`。当下限大于 1 时切换到 GStreamer
shared media，由一条编码管线原生扇出到并发客户端，避免非共享 `DESCRIBE`/编码器预热串行阻塞；
下限为 1 时仍保持重连代际隔离。两种模式都要求选定的客户端/媒体下限满足后才产生
`frame_accepted`。SDP 使用正的 `30/1` nominal rate 完成协商，回放 `captured_at_ns` 仍保持
GST PTS/DTS 的权威时间。

便携 helper 的当前四客户端验收证据为
`artifacts/evaluation/replay-rtsp-multiclient-20260903-pre.8-v9-portable-4clients-shared.json`
（3,299 bytes，SHA-256 `994818A9FE96AD5D1BF1C02286E8A1FF67181F63F203C89B7FB6CE75076308B2`）。四个并发
raw RTSP/TCP 客户端均完成 `OPTIONS → DESCRIBE → SETUP → PLAY`，H.264/正帧率 SDP、RTPv2/PT96
包增量、两个稳态 fan-out frame、EOS 和 summary，记录 `gate_passed=true`。该证据严格标注
`source_kind=software_only_local_rtsp`、`hardware_claim=none`，只关闭 RTSP transport software
子门，不改变真实相机/Jetson、室外 GNSS、Pixhawk、HIL/REAL、人工 Fly View 或最终
`v0.3.0` 发布门；此前 v4 双客户端文件保留为历史记录。

## 台式机迁移后的现场链路快照（2026-09-03，pre.8）

本轮在迁移后的台式机上重新执行了只读接口诊断和 fail-fast 现场预检。接口报告
`artifacts/evaluation/field-link-interface-diagnostic-20260903-desktop-pre8.json` 为
4,384 bytes，SHA-256
`CAB5758FA853649A74CAD3E9F7D7D3B5F718DA1F2BA8118CE58302635CFFCC6D`；活动 `Ethernet`
为 `192.168.1.13/24`，目标飞机网段 `192.168.144.0/24` 无候选接口或直连 route，
`selection.status=missing`。三采样预检
`artifacts/evaluation/field-link-preflight-20260903-desktop-pre8-interface-bound.json`
为 6,658 bytes，SHA-256
`913F6E0BB79EA0F5831B6998F8F7F9415CD380DAC6C9F00F5CA1F5A0D5A10584`；首个样本中 Jetson
`192.168.144.20:22`、GR01 `192.168.144.11:5760`、相机 `192.168.144.108:554` 均超时，
route source 仍为 `192.168.1.13`，本地 UDP `14580/14581` 可绑定，
`samples_requested=3`、`samples_collected=1`、`hardware_link_ready=false`。该报告的离线
结构/哈希/接口绑定复核在允许未就绪模式下通过，但不关闭真实现场门。

最新 pre.8 report-only E7 重算已显式绑定这份报告：清单
`artifacts/evaluation/release-gate-pre.8-current-20260903T124800Z-v6-bound.json`（17,735
bytes，SHA-256 `80BEDA6B77D76B5E3F050B3DDC7D7A035A232438B7FCBAC18159ECD2BDD0CF8F`），报告
`artifacts/evaluation/release-gate-pre.8-current-20260903T124800Z-v6-bound-report.json`
（47,754 bytes，SHA-256
`D073D15CA04C46EE02DB0B06E3704C6DC2E25F64C32C275825DAA7F820B4C464`）。E7 仍为
`gate_passed=false` / `release_allowed=false`，阻断项包括现场硬件链路、REPLAY 正向精度、
人工 Fly View、HIL/REAL 和最终 release channel；本次只更新证据时间线，不把台式机网络
诊断写成真实设备通过。

随后使用 Windows wrapper 的 `-ProtocolChecks -RequireRoute -RequireReady -StopOnFailure`
实跑同一现场窗口，报告
`artifacts/evaluation/field-link-preflight-20260903-desktop-pre8-wrapper-strict-r2.json`
（7,449 bytes，SHA-256
`A3F2037643363DCCBDC5BFC83000073AB12EA382E5385F5C0CBA56A7C815EA36`）。它记录首个样本
三端点 TCP/协议超时、route source `192.168.1.13`、`hardware_link_ready=false`，严格
wrapper 与 verifier 均返回 `1`，但失败报告没有被异常截断。最新 E7 report-only 绑定为
`artifacts/evaluation/release-gate-pre.8-current-20260903T125900Z-v6-bound.json`（17,737
bytes，SHA-256 `D0D71A48E44358D919E24179B1262ACDF47471D353E0AA9FF0630F746D3455F8`）及
`artifacts/evaluation/release-gate-pre.8-current-20260903T125900Z-v6-bound-report.json`
（47,758 bytes，SHA-256
`8CB65C5C607B069C4DDC9FBE9A3976C8A7F85453726A93F11B57FBC9918B5F2E`）；E7 仍保持
`gate_passed=false` / `release_allowed=false`。

现场会话 JSONL、现场捕获报告与室外目标真值 JSON/JSONL 同样调用
`autonomy.serialization.strict_json_loads()`。它接受 Windows UTF-8 BOM，同时拒绝重复对象
成员及 `NaN/Infinity`；捕获器按行记录 `invalid_json`，报告 verifier 会重新解析复制输出并
重算 `DET → TRK → LCK → TGT` 与 WGS-84 真值连接，不能依赖旧摘要或旧 `gate_passed` 绕过门。

本轮 report-only E7 产物为
`artifacts/evaluation/release-gate-pre.8-current-20260903T084500Z-v6-bound.json`（17,723
bytes，SHA-256 `818A77FAB6F7C5AC6735726438CB16E447D16A7058A7C17E7611E8CC4438ED9D`）及
最新 report-only 报告
`artifacts/evaluation/release-gate-pre.8-current-20260903T103906Z-v7-alert-link-bound-report.json`
（47,716 bytes，SHA-256
`BD79B0B83FB46C2A531327B273AC5E40A8566FBF52A9F0D49780D497FEA4FF64`）。该绑定使用当前
parity 的 external SIM join `simulator-external-runtime-ai-evidence-20260903-current-provider-fallback-parity.json`
及其独立 revalidation；门状态仍为
`gate_passed=false` / `release_allowed=false`，不会把 SIM 或离线证据升级成最终发布通过。

## REAL authenticated alert-link 绑定（pre.8）

`scripts/run_alert_link_bench.py --hardware-mode` 通过后，可在生成同一份 E7 清单时传入
`--real-alert-link-bench <alert-link-bench.json>`。`release_gate_manifest.py` 会把报告路径、
SHA-256、字节数、设备编号、具体非回环 IP/UDP 端口、round-trip/丢包/ACK P95 摘要、canonical
checks 和 reasons 写入顶层 `real_alert_link`；它只接受 `alert_link_bench_passed` 与
`authenticated_ack_verified=true` 的硬件域报告，不接受 loopback software baseline。

`check_release_gate.py` 每次读取清单都会重算源报告 SHA-256、严格解析 JSON、重新运行
`verify_alert_link_bench_report(require_passed=true)`，并逐字段比较 hash-bound 摘要。篡改
路径、字节数、端点、设备编号、认证位或阈值都会让该子门重新失败。旧 prerelease 清单缺少
该可选记录时仍可读取；切换到最终 `v0.3.0` channel 时，`real_alert_link` 必须存在且通过，
与 route-bound `real_field_link`、GNSS/目标态和其余 HIL/REAL 证据一起构成最终门。

示例装配命令（其余四模式证据、parity 和状态参数保持原流程）：

```powershell
python scripts/create_release_gate_manifest.py `
  --out artifacts/evaluation/release-gate-pre.8-alert-link-bound.json `
  --real-alert-link-bench artifacts/evaluation/alert-link-bench-<DATE>.json
```

当前 pre.8 wrapper 也支持同一参数；现场报告准备好后直接追加
`-RealAlertLinkBench artifacts\evaluation\alert-link-bench-<DATE>.json`，它会在同一次
装配中完成路径检查、SHA-256 绑定和后续 report-only 重验证。参数留空时仍保持既有
software-only prerelease 基线。

## 现场网卡诊断与预检绑定

迁移台式机或重新接线时，先用 `scripts/inspect_field_link_interfaces.ps1` 生成只读接口快照，
再把它通过 `scripts/run_field_link_preflight.ps1 -InterfaceDiagnostic <json>` 传入同一次预检。
wrapper 将快照 SHA-256、字节数、规范化 `expected_source_cidr`、候选接口和显式
`-InterfaceAlias` 写入 `interface_diagnostic`；`verify_field_link_report()` 会重新打开源文件
并逐字段复核，而不是相信报告内复制的 `verified=true`。Python 入口也支持等价的
`--interface-diagnostic` / `--interface-alias` 参数。

该绑定只增加网卡选择的 provenance，不改变原有 readiness 语义：
`selection.status=missing|ambiguous` 是有效的诊断结果，但 `hardware_link_ready` 仍保持
失败，直到三端点、协议、路由和后续 GNSS/测距/执行器证据都满足；诊断不会发包、登录或修改
IP、route、DNS。

现场报告的离线重算也采用严格 JSON 解析：重复对象成员、`NaN/Infinity` 和符号链接会被拒绝，
避免后出现的字段覆盖已记录的 readiness、probe 或绑定摘要；Windows UTF-8 BOM 仍然兼容。

当前台式机已用该绑定路径生成
`artifacts/evaluation/field-link-preflight-20260903-interface-bound.json`（6,648 bytes，SHA-256
`140054B79FD9F74008EDA8551E5EC9109B04688520F60BD2092D2E6F0E5EFDE0`）。随后执行默认
`run_current_release_gate_pre8.ps1 -Stamp 20260903T061300Z`，得到清单
`release-gate-pre.8-current-20260903T061300Z-v6-bound.json`（SHA-256
`138C5A6997B10742BF20567D983D0196A76E40FC0511C441E1AD836BFB5FF585`）和报告
`release-gate-pre.8-current-20260903T061300Z-v6-bound-report.json`（SHA-256
`427AA13F1F2FB0C552A6B4CDACCB70164989B01F6D05479407E13526E182E0C6`）。
`interface_diagnostic` 复核为通过，但现场 `hardware_link_ready=false`，因此 E7 和最终
`v0.3.0` 仍保持阻断。

严格 JSON 解析回归后，全量 Python pytest 共收集 `2555` 项并全部通过。最新 report-only
报告为 `artifacts/evaluation/release-gate-pre.8-current-20260903T062700Z-v6-bound-report.json`
（47,441 bytes，SHA-256
`B6D5C4048316F7B7D56556AAE7324572FCAEFD5916B1A04585B0D3DDA7D9CE4B`）；清单 SHA-256
仍为 `138C5A6997B10742BF20567D983D0196A76E40FC0511C441E1AD836BFB5FF585`。这只是验证器
收口后的 provenance 重算，E7 继续 `gate_passed=false` / `release_allowed=false`。

`v0.3.0` 的最终发布不能由单个 SIM、SITL、桌面 EXE 或单元测试自动推导。发布门需要
四个运行模式（`REPLAY`、`SIM`、`HIL`、`REAL`）各自提供带 SHA-256 的证据，并明确证明
以下五项在四种模式中完全相同：

```text
UI contract
core algorithm
task contract
safety policy
PX4 Mission path
```

机器汇总入口是 `multidetect-release-gate`，实现位于
`src/multidetect/release_gate.py`，schema 位于
`configs/schemas/release-gate-manifest.schema.json`。

当前（2026-09-03）导航 GPS 质量门已经随 parity 更新；其后相机标定文档、室外深度标定
文档和 JSON 样本入口也收口到严格解析。当前 `configs/release-parity.json` SHA-256 为
`0AFF3DF8913E8C99C8495FAD743F1E85D68716DE86719E1D91BA628F47343DF7`，
`core_algorithm_hash=434b86c6a8b677e60a4ae5766586b603855609e43cc0521695aa1e21995963f9`。
`NavigationFusionConfig` 不能把 fix/卫星数下限放宽到 canonical floor 以下；速度路径的
`0.75 s` freshness 仍然独立于目标落图 `1.5 s` 坐标门。最新全量 Python 回归为 `2605`
项（`2604 passed / 1 skipped`），日志为
`artifacts/evaluation/pytest-full-20260903-rtsp-multiclient-v4.log`，SHA-256
`1CF6757BB855F7D0F51B311D2BA7E528D210550A26EC73399A1B298A68028B69`。E7 当前清单为
`artifacts/evaluation/release-gate-pre.8-current-20260903T084500Z-v6-bound.json`
（SHA-256 `818A77FAB6F7C5AC6735726438CB16E447D16A7058A7C17E7611E8CC4438ED9D`），对应
最新 report-only 报告 SHA-256 `BD79B0B83FB46C2A531327B273AC5E40A8566FBF52A9F0D49780D497FEA4FF64`；报告仍
`gate_passed=false` / `release_allowed=false`。

上一轮（导航 GPS quality floor 之前）parity 与三机型 external runtime/live-AI 重新绑定后的严格软件总验收为
`artifacts/evaluation/multidetect-goal-acceptance-20260903-bearing-uncertainty.json`
（SHA-256 `04AFC8C6C61E340020FB7E89483D00111B00C8D61B799289EEDF7F2B4B2BF459`，核心测试
收集 `2536`，QGC custom `66`，`software_status=pass`，凭据
`ready=true/gate_overridden=false`）。此前 19,357-byte 快照以及时间对齐与视线不确定度
收口的 `multidetect-goal-acceptance-20260903-partial-velocity-fail-closed.json` 均作为独立
历史 provenance 保留。上一轮 E7 绑定为
`artifacts/evaluation/release-gate-pre.8-current-20260903T024500Z-v6-bound.json`
（17,696 bytes，SHA-256 `3D379F24C67F438A287D5F714CD4AEE79C6877DE67B6691EE49AF2D012258BEC`），
report-only 为 `artifacts/evaluation/release-gate-pre.8-current-20260903T024500Z-v6-bound-report.json`
（46,307 bytes，SHA-256 `C8749E0D7B5AB8C976477CF244586E734DFCEEBD5C07A8047689F62D5BCE040C`）。
清单现在把六源 external SIM runtime/live-AI join、独立 revalidation、六个 source hash
和当前 parity 一并写入顶层 `simulator_external_evidence`；报告仍为
`gate_passed=false` / `release_allowed=false`。这次只更新源码、parity 和证据 provenance，
不把 software/SIM 结果升级为现场硬件或最终发布资格；当前版本以本节严格解析收口记录为准。
日常 wrapper 的默认现场 link 输入已切换为带网卡诊断绑定的
`field-link-preflight-20260903-interface-bound.json`；该文件记录当前桌面网段预检失败，
但 `interface_diagnostic` 子校验通过，所以无参数复核会保留
`hardware_link_ready=false`，不会回退到更早的 follow-up 快照。旧的
`field-link-preflight-20260903-desktop-recheck.json` 仍作为历史 provenance 保留。

随后用不带证据覆盖参数的默认 wrapper 做固定时间回归：
`artifacts/evaluation/release-gate-pre.8-current-20260903T033000Z-v6-bound.json` 与上面的
manifest 内容和 SHA-256 完全一致（`1C6CA0FD95C8F3CA031B785CDFACC7D88AC1B48B2479F5F7238B7C4FE3D8E1E1`），
新 report-only 输出为
`artifacts/evaluation/release-gate-pre.8-current-20260903T033000Z-v6-bound-report.json`
（SHA-256 `40544892BF4A0947CA6C804BC5C1EEEEEFF8641B2C0B62312C458B00CB412279`）。wrapper
退出码为 `0` 仅表示阻断结果已成功记录；报告仍为 `gate_passed=false` /
`release_allowed=false`，未改变任何现场或发布状态。

上一轮核心算法还收口了固定翼 roll/pitch 对 NED 射线方位 `1σ` 的中心差分传播，并将目标落图
的 bearing `σθ` 按 WGS-84 地面弧长 `r·σθ(rad)` 转换。上一轮生成的 `configs/release-parity.json`
文件 SHA-256 为 `CCDCA6FA7FBE5512200B9A4DCE40F7303E6591ED43BDC33D009AE93075441C2D`，
`core_algorithm_hash=338981686f2142144f438ce34b4f91a18f6cab76b9f3d8efd238ac1e3fd525e7`；
该变化只更新共享测距/定位证据，不产生飞控输出。

external verifier 的 `release_status` 解析同时接受开发标签 `v0.3.0-pre.N` 和最终标签
`v0.3.0`；切换到最终标签不会绕过发布门，仍必须匹配 `configs/product-version.json`、
当前 parity、六源哈希重算以及 REPLAY/SIM/HIL/REAL 全部必需证据。

### 迁移后有界网段发现（2026-09-03）

在网段扫描之前，迁移后的台式机先运行了只读网卡诊断
`scripts/inspect_field_link_interfaces.ps1`。它同时列出物理接口、断开状态、IPv4 地址和
目标 CIDR 的直连 route，并通过 `selection.status` 明确 `ready`、`ambiguous` 或 `missing`；
该入口不修改 IP、route、DNS，也不把普通 SSH 候选主机提升为设备端点。当前快照
`artifacts/evaluation/field-link-interface-diagnostic-20260903.json`（SHA-256
`230598F40AC192A34E3C3094B527D33B035DFDC2D7EBE7C148FA47CC36055999`）为
`selection.status=missing`：活动 `Ethernet` 只有 `192.168.1.13/24`，目标
`192.168.144.0/24` 没有候选接口或直连 route。该结果只记录网卡层阻断，不能关闭
`hardware_link_ready`、GNSS、HIL/REAL 或最终发布门。

为排除台式机迁移导致的局域网变化，已运行一次显式的 `192.168.1.0/24` 只读发现，扫描 `254`
个地址并追加 SSH banner、被动 MAVLink 帧和 RTSP `OPTIONS` 检查。最新原始报告
[`field-link-discovery-20260903-093500-protocol.json`](../artifacts/evaluation/field-link-discovery-20260903-093500-protocol.json)
为 8,436 bytes，SHA-256
`E10B84D77128E4C0FE64FB41C9805D5757C7F5F5832086C451ABB4B653193EC4`；独立结构/来源复核
`verify_field_link_preflight.py --allow-unready` 退出 `0`。只发现四个 SSH 响应主机，
`5760/MAVLink=0`、`554/RTSP=0`，所以 `hardware_link_ready=false`。这些 SSH 主机不自动
视作 Jetson，发现也不会写入 manifest、改变端点或发送任何飞控/视频控制数据；只有在真实飞机
网段接通后，显式端点预检和 GNSS/测距/执行器现场证据才可进入最终门。

SIM 矩阵的当前故障覆盖别名为
`artifacts/evaluation/simulator-scenario-matrix-20260903-fault-coverage-pre8.json`
（SHA-256 `026EDF1BA4A4D436C9BEDF2AA61F88A2C86BD645EE632D292567E5E7C8F66307`）。它要求
`--cover-faults` 后自动扩展到 `2,611` 帧，三机型共 `9` 个声明故障事件全部触发，
重复运行 hash 一致，矩阵 verifier 退出码为 `0`。该文件与 WGS-84 严格矩阵字节一致，
只是独立运行 provenance；`truth_free/software-only` 语义不变，不关闭现场、Replay-set
accuracy、HIL/REAL、人工 Fly View 或最终 release 阻断项。

本轮还用 `scripts/run_current_release_gate_pre8.ps1` 显式绑定该矩阵，生成最新 report-only
清单 `artifacts/evaluation/release-gate-pre.8-current-20260903T133500Z-v6-bound.json`
（17,699 bytes，SHA-256
`52208E7C3EAA781369C304B1525EFA8D1FD2CEC99F2A1AE28A05B5D538656E42`）及报告
`release-gate-pre.8-current-20260903T133500Z-v6-bound-report.json`
（46,311 bytes，SHA-256
`3A9182A4E9576FF81FD248859C552DD4A5F971A996E667497B5FB0DEDC257780`）。清单中的
`simulator_scenario_matrix` verifier exit code 为 `0` 且矩阵门通过；E7 聚合仍保持
`gate_passed=false` / `release_allowed=false`，阻断项仍来自 REPLAY accuracy、现场链路、
人工 Fly View、HIL/REAL 和 prerelease channel。

## 当前 E7 绑定复核（2026-09-03，`v0.3.0-pre.8`）

本轮从显式指定的四模式 envelope、当前 WGS-84 场景矩阵和现场只读 bundle 重新生成了
[`release-gate-pre.8-current-20260902T233717Z-v6-bound.json`](../artifacts/evaluation/release-gate-pre.8-current-20260902T233717Z-v6-bound.json)
（15,971 bytes，SHA-256
`F3BF46225EA499D2BFEBF98BDCA0DD5FF6B6A4FDC7D6C2C52080A8C41441B248`）。清单绑定了当前
Replay-set/calibration、现场链路报告、SIM safety、执行器安全、WGS-84 三机型 scenario
matrix、combined SIM/QGC/Mission、PX4 Mission 事务和三份 3-D 控制记录；每个源文件都会
在聚合时重新哈希/复核。

固定时间 report-only/strict 结果为
[`release-gate-pre.8-current-20260902T233717Z-v6-bound-report.json`](../artifacts/evaluation/release-gate-pre.8-current-20260902T233717Z-v6-bound-report.json)
（64,949 bytes，SHA-256
`101A83E9AF1CD8DBFF1E1F72DD43BB97BEE2FBBA3D42324770AE24645092AB27`）；严格输出
[`release-gate-pre.8-current-20260902T233717Z-v6-bound-strict.json`](../artifacts/evaluation/release-gate-pre.8-current-20260902T233717Z-v6-bound-strict.json)
与 report-only 字节和 SHA-256 相同。结果明确为
`gate_passed=false`、`release_allowed=false`：Replay-set 仍是合成/未完成 accuracy，现场
目标网段未就绪，operator Fly View/Mission 和 HIL/REAL 硬件状态未声明。严格检查退出码为
`2`；`--report-only` 只允许记录该阻断结果，不会改变版本、现场状态或任何硬件门。

后续复核直接复用同一组显式输入，避免只更新某一个节点时误绑旧证据：

```powershell
Set-Location D:\Projects\Multi-Detect
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$evaluatedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$manifestOut = "artifacts\evaluation\release-gate-pre.8-current-$stamp-v6-bound.json"
$reportOut = "artifacts\evaluation\release-gate-pre.8-current-$stamp-v6-bound-report.json"

& $py scripts\create_release_gate_manifest.py `
  --out $manifestOut `
  --parity-json configs\release-parity.json `
  --replay-evidence artifacts\evaluation\release-mode-replay-20260901-pre.8.json `
  --sim-evidence artifacts\evaluation\release-mode-sim-20260902-pre.8-combined-qgc-v4.json `
  --hil-evidence artifacts\evaluation\release-mode-hil-20260902-target-state-evidence-v1.json `
  --real-evidence artifacts\evaluation\release-mode-real-20260901-pre.8-field-rerun.json `
  --replay-set-manifest artifacts\replay-sets\sim-gazebo-2026\replay-set.manifest.json `
  --replay-set-accuracy-evidence artifacts\replay-sets\sim-gazebo-2026\accuracy-report-20260902-precision-gate.json `
  --replay-set-calibration-verification artifacts\replay-sets\sim-gazebo-2026\threshold-calibration-20260902-pre.8-verification.json `
  --real-field-link-preflight artifacts\evaluation\field-link-preflight-20260903-desktop-recheck.json `
  --field-evidence-bundle artifacts\evaluation\field-evidence-bundle-20260903-desktop-recheck.json `
  --simulator-safety-evidence artifacts\evaluation\simulator-safety-acceptance-20260903-wgs84-strict.json `
  --actuator-safety-evidence artifacts\evaluation\actuator-safety-software-acceptance-20260903-wgs84-strict.json `
  --simulator-scenario-matrix-evidence artifacts\evaluation\simulator-scenario-matrix-20260903-wgs84-strict.json `
  --simulator-combined-evidence artifacts\evaluation\combined-sim-qgc-20260902-hwnd-fallback-v4.json `
  --simulator-combined-release-mode-evidence artifacts\evaluation\release-mode-sim-20260902-pre.8-combined-qgc-v4.json `
  --simulator-external-evidence artifacts\evaluation\simulator-external-runtime-ai-evidence-20260903-current-provider-fallback-parity.json `
  --simulator-external-evidence-revalidation artifacts\evaluation\simulator-external-runtime-ai-revalidation-20260903-current-provider-fallback-parity.json `
  --px4-airframe-transaction-evidence artifacts\evaluation\px4-airframe-mission-transaction-acceptance-20260903-wgs84-strict.json `
  --simulator-3d-control-evidence artifacts\evaluation\simulator-3d-control-acceptance-20260903-wgs84-strict-vtol.json `
  --simulator-3d-control-evidence artifacts\evaluation\simulator-3d-control-acceptance-20260903-wgs84-strict-fixed-wing.json `
  --simulator-3d-control-evidence artifacts\evaluation\simulator-3d-control-acceptance-20260903-wgs84-strict-multirotor.json `
  --allow-incomplete --no-overwrite

& $py scripts\check_release_gate.py `
  $manifestOut `
  --version-manifest configs\product-version.json `
  --out $reportOut `
  --evaluated-at-utc $evaluatedAt `
  --report-only
```

每次执行都会生成新的 UTC 时间戳输出，`--no-overwrite` 防止覆盖历史清单。现场证据和版本
发生变化后只替换对应输入与输出文件名，并重新核对源文件 SHA-256；不要把旧的
`release-mode-*` 或 `field-link-*` 文件直接复制成新的通过状态。

日常复核可以直接使用仓库封装器，它默认绑定上面列出的同一组 pre.8 输入，不扫描
`artifacts/`、不按修改时间猜测证据，并自动从 `configs/product-version.json` 生成版本后缀：

```powershell
Set-Location D:\Projects\Multi-Detect
& .\scripts\run_current_release_gate_pre8.ps1
```

如果已有同一现场窗口生成的六层 bundle，可以显式传入
`-FieldEvidenceBundle artifacts\evaluation\field-evidence-bundle-<stamp>.json`。封装器会
把它作为 `--field-evidence-bundle` 交给 manifest writer，写入时和读取时都重新验证输入
哈希与六层摘要；参数为空时不改变历史 pre.8 输入集合。

封装器默认生成 report-only 结果并返回 0，即使现场链路、REPLAY 精度、人工 Fly View 或
HIL/REAL 仍处于 pending；`-Strict` 会额外生成 `*-strict.json`，并返回严格发布门退出码（当前
待现场接通时预期为 2）：

```powershell
& .\scripts\run_current_release_gate_pre8.ps1 -Strict
```

可以用 `-Stamp yyyyMMddTHHmmssZ` 和 `-EvaluatedAtUtc yyyy-MM-ddTHH:mm:ssZ` 重放固定时间，
或用同名参数替换某一份现场证据。每个输出都先做存在性检查，时间戳冲突时直接停止，历史
清单不会被覆盖。

## 当前 parity 的三机型 external runtime/live-AI 六源绑定（2026-09-03，pre.8）

台式机 Docker Desktop 已按当前 `configs/release-parity.json` 重新执行三种机型的独立
PX4/Gazebo runtime 与 `--exercise-arm --exercise-target` live-AI source。六份 source 均使用
digest-pinned image
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`，
runtime/telemetry/cleanup、AI policy、target policy 和 target exercise gate 全部通过；
telemetry 的 GPS 为 `fix_type=3`、`satellites_visible=10`，每个 AI source `ai_steps=2`。
输出仍是共享 `SimulatorMavlinkControlAdapter` 的本机 loopback `MANUAL_CONTROL`，硬件控制
保持关闭。

文件级 join 为
[`simulator-external-runtime-ai-evidence-20260903-bearing-uncertainty.json`](../artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-bearing-uncertainty.json)
（5,365 bytes，SHA-256
`403785A43DACE03E2E9C029EBB6FE060E382001ABC32C59CD9987C43D2FFBB36`），独立 `--join`
revalidation 为
[`simulator-external-runtime-ai-revalidation-20260903-bearing-uncertainty-acceptance.json`](../artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260903-bearing-uncertainty-acceptance.json)
（545 bytes，SHA-256
`9DAC947BE5A24AEB1722677B51880D6F9EC8513CCC3F784B9D1684272CC4210B`）。两份均为
`source_recomputed=true`、`gate_passed=true`、`release_label=0.3.0-pre.8`，并绑定当前
parity SHA-256
`CCDCA6FA7FBE5512200B9A4DCE40F7303E6591ED43BDC33D009AE93075441C2D`。

对应最新严格总验收
[`multidetect-goal-acceptance-20260903-bearing-uncertainty.json`](../artifacts/evaluation/multidetect-goal-acceptance-20260903-bearing-uncertainty.json)
（45,571 bytes，SHA-256
`04AFC8C6C61E340020FB7E89483D00111B00C8D61B799289EEDF7F2B4B2BF459`）记录
`software_status=pass`、核心测试 `2536`、QGC custom `66/66`，并且 external binding
`provided=true` / `verified=true` / `source_recomputed=true` / `verification_exit_code=0`。
这只是 software-only SIM provenance；
`hardware_status=offline_pending`，真实 Jetson/相机/室外 GNSS/Pixhawk/执行器、HIL/REAL、
人工 Fly View 和真实视觉精度仍未声明，最终 `v0.3.0` 发布门继续关闭。

## 当前三机型 external runtime/live-AI 绑定（2026-09-02，pre.8）

本轮在台式机明确 Docker 路径下重新执行了三种机型的 disposable PX4/Gazebo external
runtime，并对每个场景分别产生 runtime source 与 live-AI source。VTOL、fixed-wing、
multirotor 的 runtime telemetry/GPS、external process、cleanup、AI armed heartbeat、
target policy 和 truth-free/hardware boundary 均通过；六份 source 共用 digest-pinned image
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`。

六源文件级 join 为
[`simulator-external-runtime-ai-evidence-20260902-final-six-sources-independent-v2.json`](../artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-final-six-sources-independent-v2.json)
（5,353 bytes，SHA-256
`79DBB81E4BB85366FFB7114DB2B3E9E6F921E8027133EE64603A680184B10440`），独立 `--join`
复核为
[`simulator-external-runtime-ai-revalidation-20260902-final-six-sources-independent-v2.json`](../artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-final-six-sources-independent-v2.json)
（558 bytes，SHA-256
`95A4CC58EDE1723E51B0DEBDFFECC261B6150A5374BBA49CD6B2BDEDBFFF783A`）。verifier 现在
强制六个 runtime/AI source 路径互不相同；同一路径复用会 fail-closed。两份文件均为
`source_recomputed=true`、`gate_passed=true`，当前 release label 为 `0.3.0-pre.8`；旧的
三源/旧六源合并记录仍保留为历史证据。

它们已经通过
[`multidetect-goal-acceptance-20260902-external-three-airframes-v7-independent-source-credentials-strict.json`](../artifacts/evaluation/multidetect-goal-acceptance-20260902-external-three-airframes-v7-independent-source-credentials-strict.json)
（42,100 bytes，SHA-256
`F69398FDC88D14AFA4FF0D182D0F6717108904CEDD2865DC6680CA9AD4EA5EC0`）强制绑定；该快照
收集 `2459` 个核心测试、QGC custom `66/66`，软件门通过，external 与三机型 3-D binding
均通过。goal acceptance 的独立 revalidation 为
[`simulator-external-runtime-ai-revalidation-20260902-final-six-sources-independent-v2-goal-v7.json`](../artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-final-six-sources-independent-v2-goal-v7.json)
（558 bytes，SHA-256
`DD60719E8969FA5F4D62680CBD545E9EF0A6F231CC6AA8FCEBDAAC3CF8D3FBF2`）。严格凭据模式下
`operator_credential_readiness.ready=true`、`gate_overridden=false`。这里的 external
runtime/live-AI 仅关闭软件 SIM 机器子门，`hardware_status=offline_pending`、
`operator_visual_acceptance=not_claimed`、真实硬件/室外 GNSS/HIL/REAL 仍不声明通过；
`remaining_live_gates` 只保留 ChArUco 实采、真实检测/伺服/失锁、Jetson soak 和三机型
人工 QGC Fly View 验收。

## 最新新鲜 AI source 复核（2026-09-03，pre.8）

台式机 Docker Desktop 上重新运行了 VTOL、fixed-wing、multirotor 三种 external PX4/Gazebo
场景，并以 `--exercise-arm --exercise-target` 驱动共享 live-AI 控制桥。三份新鲜 source
均记录 `gate_passed=true`、`runtime.status=passed`、GPS `fix_type=3/satellites_visible=10`、
`ai_steps=2/ai_accepted=2`、目标动作 `track` 被接受和容器 stop/remove；MAVLink
`vehicle_type` 分别为 `22/1/2`。完整文件哈希如下：

- `simulator-docker-ai-acceptance-20260903-vtol.json` — `D646C94C309A853641600D8AD5479E3BDF21176FA8B9B016A214987B99EC7193`
- `simulator-docker-ai-acceptance-20260903-fixed-wing.json` — `23468BA7A59C446F7A1D5FA212D0A981A04CCB131982EC003B05013646760375`
- `simulator-docker-ai-acceptance-20260903-multirotor.json` — `00E8428A5214383FC9D762F38B7126A973D314759A02FE6BC9EB2DD9A9106E5D`

为维持 runtime/AI 六路径 distinct，新的 join 使用旧的独立 runtime source 加上述新鲜
AI source：
[`simulator-external-runtime-ai-evidence-20260903-final-fresh-ai-v1.json`](../artifacts/evaluation/simulator-external-runtime-ai-evidence-20260903-final-fresh-ai-v1.json)
（5,338 bytes，SHA-256 `FE00BB61023738E3F3F1CEE98CA5ECA7D194A26585EFF06044140A1AEDA6FBA4`），
独立 revalidation 为
[`simulator-external-runtime-ai-revalidation-20260903-final-fresh-ai-v1.json`](../artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260903-final-fresh-ai-v1.json)
（543 bytes，SHA-256 `A03F66998AB62558595A52201847E927D0676C78D4F4D7E996C7C955212FAD42`）。两者
均 `source_recomputed=true`、`gate_passed=true`。严格凭据总验收绑定为
[`multidetect-goal-acceptance-20260903-fresh-ai-evidence-strict.json`](../artifacts/evaluation/multidetect-goal-acceptance-20260903-fresh-ai-evidence-strict.json)
（43,480 bytes，SHA-256 `8971E0D996AD39A12DE2EEC822D338B4A03E70049730BE11B74AF98534AF8903`），
`core_tests_collected=2480`、QGC custom `66/66`、`software_status=pass`、
`hardware_status=offline_pending`、`operator_credential_readiness.gate_overridden=false`。
这轮只更新软件/SIM provenance；真实 Jetson/相机/室外 GNSS/Pixhawk、人工 Fly View、HIL/REAL
和最终 `v0.3.0` 发布门仍由现场证据决定。

## 最新现场链路预检绑定（2026-09-03，pre.8）

当前台式机现场预检报告为
[`field-link-preflight-20260903-current.json`](../artifacts/evaluation/field-link-preflight-20260903-current.json)
（6,686 bytes，SHA-256 `8A6F3624D84FF11B94F8C65B87F2BDF5CABB7A6AF3A4B6075EAA39439A4E3798`）。
报告强制 `192.168.144.0/24` route：实际 source `192.168.1.13` 不在目标网段，Jetson
`192.168.144.20:22`、GR01 `192.168.144.11:5760`、相机 `192.168.144.108:554` 全部超时，
route/protocol 检查失败，仅本地 `14580/14581` UDP bind 成功；所以
`hardware_link_ready=false`、`stop_reason=failure`。严格 ready verifier 退出 `1`，
`--allow-unready` 结构/来源复核退出 `0`。

该事实已绑定到
[`multidetect-goal-acceptance-20260903-fresh-ai-field-preflight.json`](../artifacts/evaluation/multidetect-goal-acceptance-20260903-fresh-ai-field-preflight.json)
（43,758 bytes，SHA-256 `1553655AEFCCC504F0C4111F265A36C39FAD29728523EB5ECE11729ABA3080B7`）。
快照的核心测试 `2480`、QGC custom `66/66`、software pass 和 external join/revalidation
保持通过，但 `hardware_status=offline_pending`、field binding `verified=true`、
`hardware_link_ready=false`、`gate_passed=false`、`route_checks_passed=false`；凭据
`gate_overridden=false`。这是 fail-closed 现场状态，不改变 SIM 或最终发布门。

迁移台式机或更换网口时，可以用只读自动选择入口减少网卡名依赖：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\run_field_link_preflight.ps1 `
  -AutoSelectInterface -ProtocolChecks -RequireRoute -RequireReady `
  -Samples 3 -IntervalSeconds 0.5 -TimeoutSeconds 2
```

`-AutoSelectInterface` 只接受目标飞机网段内唯一的活动物理网卡；零候选或多候选直接报告
诊断并保持 `hardware_link_ready=false`，不会自动改 IP、默认路由、DNS 或显式 Jetson/GR01/
RTSP 端点。当前机器只有 `192.168.1.13/24`，所以烟测按预期退出 `1`。

随后用同一份 follow-up 报告重新执行了整体 goal-acceptance：
[`multidetect-goal-acceptance-20260903-followup.json`](../artifacts/evaluation/multidetect-goal-acceptance-20260903-followup.json)
（43,370 bytes，SHA-256
`8E448A74D4606A681CC225CB6D106A7AFD86B692D1FA9B7FB97E1A678BECFC1A`）。这次绑定的
`software_status=pass`、核心测试 `2484`、QGC custom `66/66` 和现有 SIM/external 证据均已
重新验证；field-link 报告本身 `verified=true`，但 `hardware_link_ready=false`、三端点超时、
route 源地址仍不属于 `192.168.144.0/24`。因此 `hardware_status=offline_pending`，剩余
真实检测、Jetson soak、舵机/失锁、三机型人工 Fly View 和最终 release channel 继续关闭。

## Replay-set readiness intake（2026-09-03，pre.8）

为避免现场收集六层数据时只看到一个笼统的 `replay_set=false`，新增只读命令
`scripts/check_replay_set_readiness.py`。它逐层重算 manifest 引用的 raw/truth/prediction
哈希和帧数，列出 `deployment_recording`、独立标注、预测流以及可计算 precision/recall
所需正例分母；追加 `--accuracy-report` 时还会根据报告内门限重新运行 evaluator 并比较
关键字段。命令不会修改 manifest、标签或模型，也不会打开任何现场设备：

```powershell
$py = 'D:\Projects\Multi-Detect\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts/check_replay_set_readiness.py `
  artifacts\replay-sets\sim-gazebo-2026\replay-set.manifest.json `
  --report-only `
  --out artifacts\evaluation\replay-set-readiness-20260903-pre.8.json
```

当前报告
[`replay-set-readiness-20260903-pre.8.json`](../artifacts/evaluation/replay-set-readiness-20260903-pre.8.json)
（9,379 bytes，SHA-256 `77D65F2EF5596DAAA7135B7E91CD9927DF5EF70BF43C97CC6F64BB67D7EF6414`）的
`gate_passed=false` 是正确结果：SIM 六层结构和哈希完整，但来源仍为
`synthetic_demo`、标注未独立复核、真值/预测没有正例。该 readiness 只把阻塞项和下一步
动作显式化，不把 SIM 空基线升级为部署域精度证据；替换成六层真实部署记录后，仍需重新
生成 manifest、运行 `evaluate_replay_set.py --reviewed --reviewer <id>`，再绑定最终 E7
accuracy evidence。

从 `v0.3.0-pre.8` 起，accuracy evidence 还必须覆盖五个正向部署分层：
`day`、`night`、`haze`、`reflection`、`smoke_only`。每层都要有独立复核的真值对象和
同一 pinned detector 的预测；`no_fire` 只作为负例控制，不充当正例分母。报告中的
`positive_strata_coverage_passed`、`positive_stratum_coverage` 与 readiness 的
`positive_truth_by_stratum` / `positive_prediction_by_stratum` 是同一契约的三种视图。
E7 writer 和 evaluator 会在声称 `gate_passed=true` 或 `accuracy_validated=true` 时重新检查
分层列表、字段和非零计数，缺失或篡改都会保持 `replay_set.passed=false`。

本轮针对 SIM 空基线的新诊断报告为
[`accuracy-report-20260903-positive-strata.json`](../artifacts/replay-sets/sim-gazebo-2026/accuracy-report-20260903-positive-strata.json)
（SHA-256 `7DE80E0E43D669A3D8DD0D0A4D625CBDCF86B23C25B7580C2ABDEBF0848500CF`）和
[`replay-set-readiness-20260903-positive-strata.json`](../artifacts/evaluation/replay-set-readiness-20260903-positive-strata.json)
（SHA-256 `38EFF80B758CBDED613FA6A882DB20AE97A00FA9A00A7DB76B1328CBF32C4018`）。两者均
`gate_passed=false`，因为当前来源是 `synthetic_demo`，没有人工正例/预测；该结果是预期的
fail-closed 状态，不能写成真实部署域 accuracy 通过。

使用新分层报告重绑的当前 E7 report-only 产物为
[`release-gate-pre.8-current-20260903T123000Z-v6-bound.json`](../artifacts/evaluation/release-gate-pre.8-current-20260903T123000Z-v6-bound.json)
（SHA-256 `5BD9E2800DC4DC2C4440880347AD2C7A5D95A915B03FBFC274CED8B6CE9470D3`）和
[`report`](../artifacts/evaluation/release-gate-pre.8-current-20260903T123000Z-v6-bound-report.json)
（SHA-256 `D66D22FEF31E29BCF9347D9AC3AF4DB41E14AC782D1AB0EE952C479B0F6BBD07`）。wrapper 退出码
为 `0` 但门状态仍为 `gate_passed=false` / `release_allowed=false`，符合当前现场与最终发布门
未完成的事实。

同日又执行了一轮有界、只读现场链路 follow-up，报告为
[`field-link-preflight-20260903-followup.json`](../artifacts/evaluation/field-link-preflight-20260903-followup.json)
（5,895 bytes，SHA-256
`951CAC7DD09775904F6E56CDC9D70D00C916B0D803EABBB77F040F6A0DD00BB4`）。台式机当前
Ethernet 地址为 `192.168.1.13/24`；Jetson/GR01/RTSP 三个显式端点均超时，且 route 源地址
不在期望的 `192.168.144.0/24` 内。本次 wrapper 使用 `--allow-unready` 仅验证了报告结构和
摘要一致性，实际 `hardware_link_ready=false`、`all_passed=false`；该失败事实继续保持
REAL 门关闭，接线后应使用真实端点和 `-ProtocolChecks -RequireRoute -RequireReady` 重跑。

随后又用同一组严格参数执行了最新 continuation 预检：
[`field-link-preflight-20260903-continuation.json`](../artifacts/evaluation/field-link-preflight-20260903-continuation.json)
（16,803 bytes，SHA-256
`98E3D9BA1ECEBB6D41E61CBB54B6DE1E0B648BC86D4554C683E4D1526D2EFF98`）。结果仍为
`hardware_link_ready=false` / `all_passed=false`：Ethernet 为 `192.168.1.13/24`，而期望飞机
网段为 `192.168.144.0/24`；Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760`、
相机 `192.168.144.108:554` 均连续超时，只有本地 UDP `14580/14581` bind 成功。该报告已绑定
到上面的 `20260903T123000Z` E7 report-only 清单，仍不改变 REAL/HIL 发布门状态。

随后用新正向分层 accuracy report 重跑独立总验收，输出为
[`multidetect-goal-acceptance-20260903-positive-strata-v1.json`](../artifacts/evaluation/multidetect-goal-acceptance-20260903-positive-strata-v1.json)
（52,222 bytes，SHA-256
`ADF74A58DCF7804C3DA4D0C02A8DBFC878F06B450BBF2660E0601D06E2C2760D`）。核心测试收集
`2544`、QGC custom `66/66`、软件门为 `pass`；其绑定的 readiness
[`replay-set-readiness-goal-acceptance-20260903-positive-strata-v1.json`](../artifacts/evaluation/replay-set-readiness-goal-acceptance-20260903-positive-strata-v1.json)
（SHA-256 `38EFF80B758CBDED613FA6A882DB20AE97A00FA9A00A7DB76B1328CBF32C4018`）明确
`gate_passed=false`，因为六层仍来自 `synthetic_demo`，五个正向分层没有独立复核真值/预测。
该总验收的 external SIM 与三机型确定性控制子门保持通过，现场链路仍
`hardware_link_ready=false` / `offline_pending`；所以 HIL/REAL、人工 Fly View 和最终
`v0.3.0` release channel 继续关闭。

## Goal acceptance 的 REPLAY + external SIM 子绑定（2026-09-03，pre.8）

`scripts/run_goal_acceptance.ps1` 现在可以把 REPLAY readiness 直接嵌入总验收，而不是只在
E7 `release-gate` 清单中单独查看。传入 `-ReplaySetManifest` 后，脚本会调用
`scripts/check_replay_set_readiness.py`，重算 manifest/raw/truth/prediction 哈希，接受严格
模式的 `0/2` 返回码（`2` 代表 readiness 仍 pending），并在
`replay_set_readiness` 节点记录 manifest/readiness/accuracy 路径与 SHA-256、数据集版本、
`manifest_gate_passed`、`accuracy_gate_passed`、`gate_passed`、`blocking_reasons` 和
`next_actions`。加 `-RequireReplaySet` 可把 pending readiness 提升为脚本失败，适合最终
发布前的硬门。

当前复核命令和输出：

```powershell
$py = 'D:\Projects\Multi-Detect\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\run_goal_acceptance.ps1 `
  -ReplaySetManifest artifacts\replay-sets\sim-gazebo-2026\replay-set.manifest.json `
  -ReplaySetReadinessOutput artifacts\evaluation\replay-set-readiness-goal-acceptance-20260903-v6.json `
  -EvidencePath artifacts\evaluation\multidetect-goal-acceptance-20260903-replay-readiness-v6-strict.json `
  -FieldLinkPreflight artifacts\evaluation\field-link-preflight-20260903-followup.json `
  -SimulatorExternalEvidence artifacts\evaluation\simulator-external-runtime-ai-evidence-20260903-final-fresh-ai-v1.json `
  -SimulatorExternalRevalidationOutput artifacts\evaluation\simulator-external-runtime-ai-revalidation-20260903-final-fresh-ai-goal-v6.json `
  -CombinedSimEvidence artifacts\evaluation\combined-sim-qgc-20260902-hwnd-fallback-v4.json `
  -CombinedSimReleaseModeEvidence artifacts\evaluation\release-mode-sim-20260902-pre.8-combined-qgc-v4.json `
  -RequireSimulatorExternalEvidence `
  -RequireCombinedSimEvidence `
```

总快照为
[`multidetect-goal-acceptance-20260903-replay-readiness-v6-strict.json`](../artifacts/evaluation/multidetect-goal-acceptance-20260903-replay-readiness-v6-strict.json)
（56,347 bytes，SHA-256 `CD979D0C0E6DE1FC6C777D0592049DDB289DE7E3DEAB1D2D4DCDA4AA36937AAD`），
其中核心测试 `2486`、QGC custom `66/66`、脚本退出码 `0`，严格凭据
`ready=true/gate_overridden=false`，external join 的 `source_recomputed=true`、combined SIM
gate 也为 `true`，但嵌入 readiness 的严格返回码为
`2`、`gate_passed=false`。当前 SIM readiness
[`replay-set-readiness-goal-acceptance-20260903-v6.json`](../artifacts/evaluation/replay-set-readiness-goal-acceptance-20260903-v6.json)
（9,379 bytes，SHA-256 `77D65F2EF5596DAAA7135B7E91CD9927DF5EF70BF43C97CC6F64BB67D7EF6414`）明确
六个条目仍是 `synthetic_demo`、标注未复核、没有正例和 accuracy report。该绑定只是把
REPLAY 缺口固定进总证据；external/combined 通过的是 software-only 子门，绝不把它们
提升成部署域精度、室外 GNSS、真实硬件、HIL/REAL 或最终 `v0.3.0` 通过。当前剩余的
SIM 相关 live gate 只剩人工 QGC Fly View，真实现场与部署域门仍保持独立。

## 执行器安全软件子门（2026-09-02，pre.8）

固定翼软件验收链新增了一个独立、可重算的执行器契约。运行：

```powershell
$py = 'D:\Projects\Multi-Detect\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts/run_actuator_safety_acceptance.py `
  --out artifacts/evaluation/actuator-safety-software-acceptance-20260902-pre8.json
& $py scripts/verify_actuator_safety_evidence.py `
  artifacts/evaluation/actuator-safety-software-acceptance-20260902-pre8.json `
  --expected-release-label 0.3.0-pre.8
```

当前证据 `artifacts/evaluation/actuator-safety-software-acceptance-20260902-pre8.json`
为 2,226 bytes、SHA-256
`8AD952BF8EBFDDFCF716EE11F212161B2869EDFA929CFED549657C3D8F58C2CC`，其
`gate_passed=true` 覆盖：

- 左右舵面 `direction=+1/-1` 的语义方向和 `[-1,1]` 输入域；
- 每个通道的绝对 `minimum/neutral/maximum` PWM 行程；
- 单次 cancel 与 lost-lock 都发出 canonical neutral 并清除目标绑定，lost-lock 恢复
  进入前置模式；
- watchdog 只产生一次 neutral 帧，进入 latched `failsafe` 后所有新命令被拒绝。

实现和 JSON schema 分别是
[`src/multidetect/actuator_safety.py`](../src/multidetect/actuator_safety.py) 与
[`configs/schemas/actuator-safety-evidence.schema.json`](../configs/schemas/actuator-safety-evidence.schema.json)。
该子门是 `software_only=true` 的 HIL-preflight：没有串口、CAN、GPIO、PWM、MAVLink 或
Pixhawk 输出，`physical_outputs_present=false`，因此不关闭剩余的真实推进器断开舵机方向/
限位、真实失锁回退、Jetson soak、人工 Fly View、HIL/REAL 和最终 `v0.3.0` 门。
本轮执行器契约后的全仓库回归为 `2467` 项、退出码 `0`；该数字只代表软件回归，不改变
现场门状态。

这份软件 bench 现在可以直接绑定到 E7 清单顶层 `actuator_safety`：

```powershell
$py = 'D:\Projects\Multi-Detect\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
& $py scripts\create_release_gate_manifest.py `
  --actuator-safety-evidence `
    artifacts\evaluation\actuator-safety-software-acceptance-20260902-pre8.json `
  ...四个模式 evidence 与 parity 参数...
```

writer 写入相对路径、源文件 SHA-256、`runtime_mode=HIL`、
`software_only=true`、`physical_outputs_present=false`、推进器状态标记、八项 canonical
check 以及 verifier 事件。聚合器每次读取 E7 清单都会重新哈希源文件、调用同一
`verify_actuator_safety_evidence` 并比较声明 check；源文件、边界字段、版本或 check map
任一漂移都会 fail-closed。pre-release 可省略该字段以兼容历史清单，最终 `release` channel
则强制要求通过的 `actuator_safety` 绑定。这个节点只关闭共享执行器映射/状态机的软件子门，
不替代推进器断开后的真实舵机方向/行程/失锁回退、真实 Pixhawk/HIL/REAL 或人工 Fly View。

执行器契约接入后的严格总验收快照为
[`multidetect-goal-acceptance-20260902-actuator-safety-external-strict.json`](../artifacts/evaluation/multidetect-goal-acceptance-20260902-actuator-safety-external-strict.json)
（43,745 bytes，SHA-256
`68F4B7565197E58B9346B668CE27234896D32DFA44A9E909FD03355C7B5B6C26`）。它收集
`core_tests_collected=2467`、QGC custom `66/66`，绑定六源 external runtime/live-AI 与三机型
3-D deterministic evidence，且 `operator_credential_readiness.ready=true`、
`gate_overridden=false`。快照中的执行器节点为独立验证的
`gate_passed=true`、`software_only=true`、`physical_outputs_present=false`；现场预检虽
`verified=true` 仍报告 `hardware_link_ready=false`，因此剩余真实舵机/失锁、ChArUco 实采、
真实检测闭环、Jetson soak、人工 Fly View、HIL/REAL 和最终 `v0.3.0` 门不变。

## 当前 E7 清单与双重复核（2026-09-02，pre.8）

为把最新六源 provenance、三机型 3-D control 和现场只读状态固定到当前发布门，重新生成了
[`release-gate-pre.8-current-20260902-v7-independent-source.json`](../artifacts/evaluation/release-gate-pre.8-current-20260902-v7-independent-source.json)
（14,068 bytes，SHA-256
`5FCF05088BD73F03231BB8B0FDD49B74F79BC1C2849D2D4292BF98913794A8AB`）。该 manifest 已通过
[`release-gate-manifest.schema.json`](../configs/schemas/release-gate-manifest.schema.json)；
它绑定 REPLAY/SIM/HIL/REAL envelope、field-link preflight、三机型 3-D control、SIM
safety/matrix、combined SIM/QGC/Mission、PX4 airframe transaction，以及顶层
`simulator_external_evidence` 的六源 runtime/AI join、独立 revalidation、source hash 和
当前 parity。writer 与 evaluator 都会重新计算该 join；goal-acceptance 中的同名节点继续
作为软件总验收 provenance。旧的 pre.8 清单可以省略该顶层字段以兼容历史 report-only，
最终 `release` channel 则强制要求通过的 E7 六源绑定。

对同一清单执行了两条 CLI 路径：

- report-only：
  [`release-gate-pre.8-current-20260902-v7-independent-source-report.json`](../artifacts/evaluation/release-gate-pre.8-current-20260902-v7-independent-source-report.json)，
  40,740 bytes，SHA-256
  `AF5DBA49793813E3B1A52F40814CDEB2DFCE1727B246D4DAFC8BE4A89EE8281A`，退出码 `0`；
- strict：
  [`release-gate-pre.8-current-20260902-v7-independent-source-strict.json`](../artifacts/evaluation/release-gate-pre.8-current-20260902-v7-independent-source-strict.json)，
  40,740 bytes，SHA-256
  `568E8F34073CD31A3096C6A51DECCB70222CC462047DA32E329C7CA12DC0C441`，退出码 `2`。

两份输出均为 `gate_passed=false`、`release_allowed=false`，报告含 31 条阻断原因：REPLAY
精度与部署覆盖、现场硬件链路、HIL/REAL、人工 Fly View 以及 prerelease channel 仍未
满足。严格退出码 `2` 是预期的 fail-closed 结果；这轮刷新只更新 provenance，不改变硬件
门状态，也不把最终版本推进为 `v0.3.0`。

该总验收同时绑定 22:00 后的只读现场预检
[`field-link-preflight-20260902-after-2200.json`](../artifacts/evaluation/field-link-preflight-20260902-after-2200.json)
（6,692 bytes，SHA-256
`37D68E88AB7433898BE77FED696009564F558274B7824EB0D3164D5FC563200A`）。报告本身结构有效，
本地 metadata/QGC UDP 端口可绑定，但 Jetson `192.168.144.20:22`、GR01
`192.168.144.11:5760`、相机 `192.168.144.108:554` 与期望 `192.168.144.0/24` route 均失败，
因此 `hardware_link_ready=false`；现场硬件门仍按 fail-closed 处理。

### 2026-09-03 执行器安全顶层绑定（pre.8）

在上述历史清单之后，E7 writer/evaluator/schema 已加入共享执行器安全绑定。最新清单为
[`release-gate-pre.8-current-20260903-actuator-bound.json`](../artifacts/evaluation/release-gate-pre.8-current-20260903-actuator-bound.json)
（14,980 bytes，SHA-256
`393A329AE46944E20035D899FCF5BD7686183B71E172B132F62137CCA1071F65`）。它绑定
`actuator-safety-software-acceptance-20260902-pre8.json`（源 SHA-256
`8AD952BF8EBFDDFCF716EE11F212161B2869EDFA929CFED549657C3D8F58C2CC`），并且聚合器重新
验证 `actuator_safety.present=true`、`passed=true`、八项 check 全真和 HIL/software-only
边界。固定时间 report-only 与严格输出均为 42,498 bytes、SHA-256
`94878B1D5230D26CAC56B5049251603F7BA7787FCD4D3ADC1345A021AE2F4355`，退出码为 `0`/`2`；
整体 `gate_passed=false`、`release_allowed=false` 仍由 REPLAY、现场链路、人工视觉、
HIL/REAL 与 prerelease channel 正确阻断。最终 `release` channel 现在还会额外要求该
顶层执行器绑定，避免只在 goal acceptance 中出现的软件节点被遗漏。

## 最新 combined SIM/QGC/Mission 证据（2026-09-02，pre.8）

当前 portable QGC 以完整窗口重跑了 `900` 帧联合验收，使用 `MissionTimeoutSeconds=240`、
`MissionSetupAllowanceSeconds=120`、`RouterDurationSeconds=420` 和 `FixedWingHoldSeconds=12`。
source 为
`artifacts/evaluation/combined-sim-qgc-20260902T071623Z-66836/combined-evidence.json`
（250,024 bytes，SHA-256
`38E179075340A41CF040406DC89F7CF106EC1282374AD3B6376329C17CB6191B`），独立 verifier
`combined-evidence-verification.json` 为 2,567 bytes、SHA-256
`DC06C1CC646EE37C94FC644AD59DF49074A688ECB361F69A206E475354430419`，退出码 `0`，
`gate_passed=true`。

Mission 观察窗口为 `117.938 s`：`armed`、多旋翼起飞、固定翼转换、Mission reached 序列
`0,1,2,3,4,5,6`、返程降落和最终 `disarmed` 均被记录；current telemetry 观察到
`0,2,3,4,5,6`，transition item `1` 由 transaction/journal 确认。最大相对高度 `25.423 m`、
最大离家距离 `127.632 m`。原生 QGC fixed-wing `videoContent` QQuickItem、transport binding
和 AUTO Mission 截图也在同一 QGC PID 下生成；动态 loopback 端口、Docker 清理、Mission
journal 和 Gazebo camera/CameraInfo/NavSat readiness 均通过。该证据只关闭 software-only
SIM/QGC/Mission 子门，保留 `operator_visual_acceptance=not_claimed` 与硬件/REAL/HIL 未声明。

该 source 的 pre.8 envelope 为
`artifacts/evaluation/release-mode-sim-20260902-pre.8-combined-qgc-v5.json`（579 bytes，
SHA-256 `7434FE3E3C47A947B58C53F49AE1E083CEFBF871F3B03A06A0417E21EFBE6493`）。绑定它的新
E7 清单为
`artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v5.json`（10,879 bytes，
SHA-256 `1340AC8CCCB62CB02F4F82B20DBAEA8C570353C10C18D719D951A430DD46402A`）。固定时间
`2026-09-02T07:30:00Z` 的 report-only/strict 复核均为 22,968 bytes、SHA-256
`E8051A6C7BD68DD793ACAB6B77415BF2D44915C5474952D23F95476CFE18C847`，退出码 `0`/`2`；
整体 `gate_passed=false`、`release_allowed=false` 的阻断原因仍包括 REPLAY 精度、现场
硬件链路、HIL/REAL、人工视觉和 prerelease channel。

这次 source 同时绑定到独立 goal acceptance
`artifacts/evaluation/multidetect-goal-acceptance-20260902-combined-qgc-v5.json`（39,499
bytes，SHA-256
`75340E0AACCFBF41538DA7B024C9484BA757090862867E92D6CDC4C62D10B169`）。该快照的核心测试
收集 `2421`、QGC custom `66/66`、QGC protocol self-test、ruff、compileall 和 diff-check
均通过，`software_status=pass`；它明确保留现场 `hardware_status=offline_pending`、
`hardware_link_ready=false` 以及剩余 REAL/HIL/人工视觉门。

## external runtime + live-AI 绑定（历史 parity 快照，2026-09-02，pre.8）

为避免总验收遗漏已经通过的三机型软件证据，当时重新绑定了 VTOL、fixed-wing、multirotor
各一份 runtime 与一份 `--exercise-arm --exercise-target` AI 报告。当时六源 join 为
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-continue-v2.json`
（5,407 bytes，SHA-256
`1F834FC5A86409E3BD338B349C9D3FC188D4FEDDEBB725E1CCBAB1DFB99722B2`，当时
`gate_passed=true`）。
用 `scripts/verify_simulator_external_evidence.py --join` 重算的绑定报告为
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-combined-qgc-v7.json`
（543 bytes，SHA-256
`206E667E1996C4E530BF5988E85F396E1824A407E565B97D445963AF4F9A49A0`，
`source_recomputed=true`、`gate_passed=true`）。

当时完整 goal acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-combined-qgc-v7.json`
（39,850 bytes，SHA-256
`082E33BEA216460271BDB44435AC6281A1F5E3F3C3F0FEC2BDDD03B9E95CD2D1`）。它收集核心测试
`2421`、QGC custom `66/66`，`software_status=pass`，并在
`simulator_external_evidence` 节点记录 `provided=true`、`verified=true`、
`source_recomputed=true`、`gate_passed=true`。该 join 只关闭 disposable PX4/Gazebo、
truth-free perception 与 AI/target policy 软件子门；`hardware_status=offline_pending`，
不改变现场 GNSS、真实 Pixhawk/Jetson/相机、HIL/REAL、人工视觉、REPLAY 精度或最终
`v0.3.0` 发布门。

随后将 REPLAY 空基线分母检查纳入新的软件验收快照：
`artifacts/evaluation/multidetect-goal-acceptance-20260902-replay-guard-v8.json`
（39,850 bytes，SHA-256
`C0CF30A37C028B3E526C0068EE10DC5A8DBDAE276F2EBCD3C6FC7B9BBD849391`）。该快照收集核心
测试 `2423`、QGC custom `66/66`、`software_status=pass`；新的空基线精度报告
`artifacts/replay-sets/sim-gazebo-2026/accuracy-report-20260902-precision-gate.json`
（9,178 bytes，SHA-256
`F474454CEE1A11F553C1A1C7EDA31D11E07D518AC1F2E898D2380FE3EC919553`）明确记录
`metrics_gate_passed=false` 与 undefined precision/recall 原因，REPLAY/现场/REAL 门仍关闭。

该空基线随后直接绑定到当前 pre.8 E7 清单
`artifacts/evaluation/release-gate-pre.8-current-20260902-replay-guard-v8.json`
（10,913 bytes，SHA-256
`8B62437E85583F244047F4CFA18D34C724F5E0C4C51DA39A66C01096EF1CE6E9`）。固定时间
`2026-09-02T08:25:00Z` 的 report-only 报告为
`artifacts/evaluation/release-gate-pre.8-current-20260902-replay-guard-v8-report.json`
（23,034 bytes，SHA-256
`1E5B1FAFA11D8058DC0C21A3FF0435CD92E7D30CF1BCDEE40C7D79DBBD8E5823`），report-only
退出码为 `0`，严格检查退出码为 `2`；两种结果均为 `gate_passed=false`、
`release_allowed=false`。E7 verifier 同时重算 replay manifest 与 accuracy artifact，
因此空/全负基线的 undefined precision/recall 不会因手工把字段改成 `true` 而穿过发布门。

## 当前 pre.8 绑定（2026-09-02）

本轮 SIM AI/GNSS 质量门收口后的 canonical parity 文件为
`configs/release-parity.json`（3,371 bytes，SHA-256
`B37A138C9CBE31E70D2A756F2B2B7A63DE81DC218F5401F2018204E01CD5E33A`），其中
`core_algorithm_hash=6c2d186e8b6c31330b16fae096c4d90302797bdf5d697e28ff4efbc55fbdc06d`、
`safety_policy_hash=f781b90021938e5b9d45056eb48c6367599401ee65f2f8f0279f0aaa03bab2d3`。
对应 E7 清单与固定时间 report-only 证据分别是
`artifacts/evaluation/release-gate-pre.8-current-20260902-ai-gps-accuracy.json`
（SHA-256 `47C5C9DA6E0F00B8B6CF4F000D1B693AEE71E632847ABD1F7C6380F06AABFE42`）和
`artifacts/evaluation/release-gate-pre.8-current-20260902-ai-gps-accuracy-report.json`
（SHA-256 `42A18B5929122FFB4F09ABC5638757F44F99587054D2827B2D2C16BEDB4A24F1`）。
report-only 结果保持 `gate_passed=false`、`release_allowed=false`：现场链路、室外 GNSS、
REPLAY 精度、HIL/REAL 与人工 Fly View 仍是未完成门，不会由软件回归自动升级。

手动控制 freshness watchdog 收口后，重新生成的首份 E7 绑定（v1）为
`artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v1.json`
（SHA-256 `55D5D65E7E4574D11BA77EF4DA1AD92EBA056CCB32ACF7242B7E1560D84381E6`），固定时间
report-only 输出为
`artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v1-report.json`
（23,063 bytes，SHA-256
`28F678330DECF412CECD30BF3073060A2113BA648F8C25D2BD79DA29E49DF4AA`）。五项 parity 均重算
为 `true`，安全证据和现场报告均经过 verifier；报告仍明确保持 `gate_passed=false`，因为
REPLAY 正样本/独立复核、现场链路、人机视觉与 HIL/REAL 尚未完成。

### 当前 `pre.8` 证据刷新（2026-09-02，manual watchdog v2）

旧 v1 清单之后，三机型 external runtime/live-AI source 已按当前 safety parity 重新 join，
并通过第二次 `--join` 重算：
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-manual-watchdog-v2.json`
（5,407 bytes，SHA-256
`6701803509C4DE7322B553034B6ABAC72D4C04836C6289C85B51C61F88652770`）和
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-manual-watchdog-v2.json`
（544 bytes，SHA-256
`A1C653C53E71F22186D2216858628575C3020200E2949C38653D8996510078D0`）均为
`source_recomputed=true`、`gate_passed=true`。旧 join 的摘要漂移来自 safety parity 更新，
已通过重新生成 source-bound join 消除。

同一批 source 重新执行完整 goal acceptance，最新快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-manual-watchdog-v2.json`
（39,946 bytes，SHA-256
`13467799B7924BED99E72DAFCEA214FBA4FFBE96C246C160B4C37772DDCE6929`）。核心测试收集
`2431`、QGC custom `66/66`、软件状态 `pass`，external binding 的 `verified`、
`source_recomputed`、`gate_passed` 均为 `true`；现场链路仍为 `hardware_status=offline_pending`。

最新 E7 清单与固定时间 report-only 报告为
`artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v2.json`
（10,481 bytes，SHA-256
`AAC4FDA23C6F47CC23D856F8B5C06F59E164B4C3222538B9B0A5D2F847D05572`）和
`artifacts/evaluation/release-gate-pre.8-current-20260902-manual-watchdog-v2-report.json`
（21,863 bytes，SHA-256
`040EC28064A4C4BCA17BA7273DB1E929B661E029A7786F7CC62D8B9846AEB848`）。五项 parity、SIM
safety/scenario/Mission 子门均通过，但总门依旧 `gate_passed=false`、`release_allowed=false`；
阻断项仍是 REPLAY 正样本/独立复核、现场链路、人工视觉、HIL/REAL 和最终 release channel。

## 当前完整 goal acceptance 续跑（2026-09-02，pre.8）

在当前 portable 构建、v4 combined SIM、三机型 external live-AI join 和严格现场预检之上，
又完整执行了一次 goal acceptance。快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-continue-pre8.json`
（39,771 bytes，SHA-256
`CEC975A754E7F4F918C50F76F92EB46D0B85D7F41645FE35BDC8F9F5E75CF7CC`）。项目测试收集
`2418`、QGC custom `66/66`、`ruff`、`compileall`、`git diff --check` 和 QGC protocol
self-test 全部通过；combined SIM 与 external live-AI join 的源文件均重新哈希并复核为
`gate_passed=true`。

这次快照的总判定仍是 `software_status=pass`、`hardware_status=offline_pending`：现场
预检绑定 `field-link-preflight-20260902-131515.json`，三台设备端点超时，route 仍来自
本机 `192.168.1.13` 而非 `192.168.144.0/24`，所以 `field_link_ready=false`。REPLAY
精度/部署覆盖、室外 GNSS、真实 Pixhawk/执行器、HIL/REAL、人工 Fly View 和最终 release
channel 仍未满足；开发机 key 的存在不改变这些门。该续跑只更新软件 provenance，不把
SIM、Docker、静态测试或诊断报告升级为 REAL 或最终 `v0.3.0`。

同一批 mode evidence 重新生成的 E7 清单为
`artifacts/evaluation/release-gate-pre.8-current-20260902-continue-pre8.json`
（10,844 bytes，SHA-256
`8470F5AE136154D264D273D1E7D3446370B7A83CCC3BC0ECB809291E4A37D0AA`）。以固定时间
`2026-09-02T05:40:00Z` 生成的 report-only/strict 报告均为 22,921 bytes、SHA-256
`29DF553263AC38AC944C9209ABB5C56CFCEBD8298FAAB1B843C65BBD8BA776EE`；退出码分别为
`0`/`2`，两者都明确 `gate_passed=false`、`release_allowed=false`，并保留 31 条阻断
原因。这样可以确认新 snapshot 不会因旧清单、旧摘要或 prerelease channel 而误放行。

### 台式机严格凭据模式回归（2026-09-02，pre.8）

补齐 CPU `onnxruntime=1.29.0`、`protobuf=4.25.8` 后，`pytest --disable-warnings -ra` 完整回归
为 `2449 passed`、退出码 `0`，原始日志及 SHA-256 见
`artifacts/evaluation/pytest-full-20260902-onnxruntime.log` /
`4F5360DEC8E64B9C8B71CDF9BEF3ACEF7FB8C7FE87C7CD4FF7361B4B2C296012`。

随后未使用 `--AllowMissingOperatorCredentials` 覆盖，重新执行 goal-acceptance，快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-onnxruntime-credentials.json`
（31,175 bytes，SHA-256
`F17D535362B2F18351178A7F9865766A3EAA3D015AC3392C2C437129C0750A52`）。它记录核心测试收集
`2449`、QGC custom `66`、QGC protocol self-test `pass`、`software_status=pass`，并确认
`operator_credential_readiness.ready=true`、`gate_overridden=false`。该快照仍明确
`hardware_status=offline_pending`、`operator_visual_acceptance=not_claimed`；REPLAY 精度、
真实 Jetson/相机/GNSS/Pixhawk、HIL/REAL、人工 Fly View 与最终 release channel 继续阻断
`v0.3.0`，不把软件回归写成现场或最终发布证据。

## 当前现场网段发现续跑（2026-09-02，pre.8）

在台式机 `Ethernet=192.168.1.13/24` 上对显式 `192.168.1.0/24` 做了有界只读服务发现，
254 个地址中 4 个 SSH 端口响应，MAVLink `5760` 与 RTSP `554` 均无命中；期望的
`192.168.144.0/24` 路由仍未出现。发现报告为
`artifacts/evaluation/field-link-preflight-20260902-discovery-continue.json`（8,542 bytes，
SHA-256 `6974A7410B5779E71CDDF39C158F2356BF6C67A394E29433C528EEB16062607C`），
 `hardware_link_ready=false`。

同一网段随后启用 `ProtocolChecks` 进行了第二次只读发现，报告为
`artifacts/evaluation/field-link-preflight-20260902-discovery-protocol-continue.json`
（9,402 bytes，SHA-256
`3D8CBBF0E6F2A2B40966BB31694700C394FBFF49050FA57B5DB2A792B63D95CF`）。4 个响应主机
均只返回 SSH banner（`192.168.1.1/.62/.63/.77`），MAVLink `5760` 和 RTSP `554` 均为
0 命中；显式 Jetson/GR01/相机端点和期望 `192.168.144.0/24` route 仍失败，报告中的
`protocol_checks_passed=false`、`hardware_link_ready=false` 保持 fail-closed。该文件
仅作为未绑定诊断 provenance，不进入当前 goal acceptance/E7 的硬件证据绑定，也不自动
选择或改写任何端点。

 该报告已绑定到新的完整 goal acceptance
`artifacts/evaluation/multidetect-goal-acceptance-20260902-discovery-continue-pre8.json`
（31,413 bytes，SHA-256
`83B90A14ADFCDEF7456F87B1F3357D25EB17D95830FB2570F75E696050E8D099`）。随后按最新
PX4 Mission artifact SHA 重新装配 E7 清单
`artifacts/evaluation/release-gate-pre.8-current-20260902-discovery-continue.json`
（10,855 bytes，SHA-256
`372EA72BAB5B116A394F5168BF46FFE61827A1BFA4B3B934C2DFF8DA9BF12BB8`）。固定时间
`2026-09-02T06:50:00Z` 的 report-only/strict 输出均为 22,905 bytes、SHA-256
`1020954A94282AB4C3A8399A50DC3F74664C1262E610C921AB8E29ACD9EEA3F8`，退出码 `0`/`2`；
PX4 Mission、SIM safety、三机型矩阵和 combined SIM 子门通过，整体
`gate_passed=false`、`release_allowed=false`，31 条阻断原因保持 fail-closed。该续跑不
把局域网 SSH 候选机、dry-run SIM 或静态回归升级为真实硬件、室外 GNSS、HIL/REAL 或
人工 Fly View 证据。

端点 wrapper 修复后的最新总验收快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-wrapper-endpoint-continue-pre8.json`
（39,814 bytes，SHA-256
`3C3F036E62FDA5C7314AE142F0714F502C6E40599013E81EFD1D5ACCA95A8ED5`）。当前 E7 清单也已
重新绑定显式端点报告，文件为
`artifacts/evaluation/release-gate-pre.8-current-20260902-wrapper-endpoint-continue-pre8.json`
（10,870 bytes，SHA-256
`8FE2FAC78FFAC69140C3693CA902B32F8BEEA2CAEABF9456464F75FD96616CE4`）。固定时间
`2026-09-02T06:05:00Z` 的 report-only/strict 输出均为 22,947 bytes、SHA-256
`24B8247077C2CBC9DB6E85FA688712FB62D0CDC38E337FD02544A08C62C07C9F`，退出码 `0`/`2`；
现场 `hardware_link_ready=false`，REPLAY 精度、HIL/REAL 和人工视觉门仍保持阻断。

在直接测距路径增加时间、来源唯一性和向下射线 fail-closed 门后，重新执行的最新总验收
快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-direct-range-safety-pre8.json`
（39,808 bytes，SHA-256
`CEEBFAF6B0D0006C64E337A98FB9E30D153FCB1127C4B8A9CE38D0AFCFAC295`）。该快照的
`software_status=pass`、核心测试 `2421`、QGC custom `66/66`，但仍准确记录
`hardware_status=offline_pending`；直接测距新增回归原因为
`duplicate_direct_range_source`、`target_ray_does_not_intersect_ground_safely` 和非法
评估时间。它只更新软件 provenance，不替代真实 Jetson/GNSS/Pixhawk、REPLAY 精度、
HIL/REAL 或人工 Fly View 证据，最终 release gate 继续关闭。

该轮重跑同时修正了 E7 清单对三机型 PX4 Mission artifact 的旧 hash 绑定。当前清单为
`artifacts/evaluation/release-gate-pre.8-current-20260902-direct-range-safety.json`
（10,870 bytes，SHA-256
`87B88E5FAF2AB25435CD74272B4F9EB9F52D5F24FFC9D57F51523D79A11E7A6E`），其 PX4 source
绑定为 `99AFC6FEA5BA0CD19F1FA6DB23570C316FE23F0F92002BEF053E4C3C08B3F0A9`。固定时间
`2026-09-02T06:25:00Z` 的 report-only/strict 复核均为 22,936 bytes、SHA-256
`165BF83E476FE9F9CA79186950EAD49FF1E9EAFEE405E275D71AD5A1F35FA60C`，退出 `0`/`2`，
`gate_passed=false`、`release_allowed=false` 和 31 条阻断原因均保持预期状态。

## pre.8 录像/GNSS 回放接线与当前构建（2026-09-02）

本轮把现场只读 `GPS_RAW_INT/GPS2_RAW` capture 接到录像回放：
`gnss_capture_sidecar.py` 先校验 capture report，再以显式 monotonic origin/offset 生成严格
`RawReplayTelemetrySample` JSONL；`replay_telemetry_join.py` 对每帧使用 latest-at-or-before
样本，拒绝未来、重复键、非有限数值和 stale 覆盖，并重算 canonical GPS quality。源录像若
已有 `scenario_id`/`scenario_manifest_hash`/`scenario_seed`，这组三元组会保留在 joined
manifest 与新的 recording identity 中。该链路仍明确是 software-only 导航回放，
`deployment_domain_evidence_complete=false`、`accuracy_validated=false`、`gate_passed=false`。

对应全量 Python 回归为 `2418 tests collected`、退出码 `0`，`ruff` 与 `compileall` 同样退出
`0`。Release 构建脚本以 `-SkipConfigure -BuildSelfTests -InstallPortable -Parallel 4` 完成，
portable 入口为
`build/windows-v0.3.0-pre.8-release/portable-v0.3.0-pre.8/bin/MultiDetectGCS.exe`，
大小 `46,575,616` bytes、SHA-256
`4D7A024F80157E5EB8671B77D6E1817FB960ECE8A8A97D0F470A222E7F7C8E40`；Release 目录 EXE
与 portable 字节一致，simple-boot 退出码 `0`。

台式机严格三采样现场 watch 为
`artifacts/evaluation/field-link-preflight-20260902-131515.json`（16,799 bytes，SHA-256
`89C5217DB61BAF0117BA9B206810AB9E1FD696B92FD5D6A595F240E8F9BB431E`）：三台实际端点均
超时，route probe 选到本机 `192.168.1.13` 而不是期望的 `192.168.144.0/24`，本机
14580/14581 仍可绑定，故 `hardware_link_ready=false`、严格预检退出 `1`。该报告只读，
不登录 Jetson、不请求 MAVLink/RTSP 数据流、不发送飞控控制帧；真实 Jetson/相机/Pixhawk、
室外 GNSS、HIL/REAL 与人工 Fly View 仍保持 pending，最终 `v0.3.0` 不发布。

## 当前台式机 live-AI external-SIM 绑定（2026-09-02，pre.8）

本轮在同一 pinned PX4/Gazebo digest 上重新执行 VTOL、fixed-wing、multirotor 三机型
disposable external runtime，并在每个场景启用 `--execute --exercise-arm --exercise-target`。
三组 runtime 均通过 qualified telemetry、`GPS_RAW_INT fix_type=3/satellites=10`、armed
heartbeat、loopback UDP I/O 和容器清理；三组 AI 均在新鲜 armed heartbeat 后完成 ARM，记录
`ai_steps=2`、`target_exercise.accepted=true`、`decision.action=track`。输入是 truth-free
synthetic perception，`hardware_control_disabled=true`。

三机型 live-AI source 摘要：

| 机型 | artifact | bytes | SHA-256 |
|---|---|---:|---|
| VTOL | `simulator-docker-ai-acceptance-20260902-pre.8-vtol-continue-v2.json` | 133,609 | `284A2C56A0698C0DF537300BAA7CCB0DF6B538FC1EFE401328D3923E210BC362` |
| fixed-wing | `simulator-docker-ai-acceptance-20260902-pre.8-fixed-wing-continue-v2.json` | 104,297 | `FE87A9C3DC356D74AFFF2D24ABDF340054224AF74FD34D453017335DC25D539B` |
| multirotor | `simulator-docker-ai-acceptance-20260902-pre.8-multirotor-continue-v2.json` | 110,065 | `4BE8C2F263C48FEE6C9B87B246A1BD56DA86A4C4C8FAD48F565CE87DD87DDFF4` |

六份 runtime/AI source 用当前 `configs/release-parity.json` 做文件级 join，输出
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-continue-v2.json`
（5,407 bytes，SHA-256
`1F834FC5A86409E3BD338B349C9D3FC188D4FEDDEBB725E1CCBAB1DFB99722B2`，
`gate_passed=true`）。`--join` 重算输出为
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-pre.8-continue-v2.json`
（543 bytes，SHA-256
`9066B558F5F9047A0DE210AFBD5895DD66DEC395BFE38229037DC398CA1829DD`，
`source_recomputed=true`、`gate_passed=true`）。

该 join 已接入总验收快照
`artifacts/evaluation/multidetect-goal-acceptance-20260902-live-ai-continue-v2.json`
（16,939 bytes，SHA-256
`BB371E3B1242E991CC6539D64961C7F4321ADCDC1F1539C3B3C39E842A9ECEF9`）。快照中
`simulator_external_evidence.provided/verified/source_recomputed/gate_passed=true`，核心测试
`2400`、QGC custom `66/66`、`software_status=pass`；严格现场预检仍是
`hardware_link_ready=false`，所以 `hardware_status=offline_pending`，外部 AI 证据只关闭
software/SIM 子门。

同一快照引用的 deterministic 3-D 控制资产
`artifacts/evaluation/simulator-3d-control-20260902-continue-v2.json`（82,463 bytes，
SHA-256 `56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）仅证明
`gate_passed=true`、`deterministic_gate_passed=true`；`external_runtime_gate_passed=false`、
视觉和硬件仍为 `not_claimed`。

这组证据不改变 E7 的发布判定：真实 Jetson/相机/Pixhawk、室外 GNSS、人工 Fly View、
HIL/REAL、REPLAY 精度与最终 `v0.3.0` release surfaces 仍需现场证据，未满足前保持
`gate_passed=false`、`release_allowed=false`。

## 最新台式机现场预检绑定（2026-09-02，pre.8）

本轮重新执行了严格的只读现场预检，报告为
`artifacts/evaluation/field-link-preflight-20260902-continue-v4.json`（4,396 bytes，SHA-256
`0438D61B311BBA94150CF59F580DBC4A29E6B31999310E33238A1E52F1A3506C`）。预检显式要求
`192.168.144.0/24` 的本地 route，并执行 SSH banner、被动 MAVLink 和 RTSP OPTIONS 检查：
三台设备均不可达，route probe 全部选到本机 `192.168.1.13`，本地 UDP 14580/14581 仍可绑定，
所以 `route_checks_passed=false`、`hardware_link_ready=false`，严格退出码为 `1`。该报告不登录
设备、不请求数据流、不发送飞控控制帧。

该报告随后绑定到
`artifacts/evaluation/multidetect-goal-acceptance-20260902-field-preflight-v4.json`
（31,777 bytes，SHA-256
`250824C312DD0CD8326721C0B27B3B10F05F53E9644FA82CC00EC87F668905F7`）。总验收退出码为 `0`，
核心测试 `2,397`、QGC custom `66/66`、`software_status=pass`，但
`hardware_status=offline_pending`；这只是刷新现场 provenance，不能提升 HIL/REAL、室外 GNSS、
Pixhawk 或最终发布状态。与该快照同时重算的 external-SIM/live-AI revalidation 为
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-pre.8-continue-v1.json`
（543 bytes，SHA-256
`54CB1001F907F8D612411579E863CFA2F8D166C71702A707C29B8105ABBA4589`），其通过范围仍限于
disposable PX4/Gazebo 与 truth-free AI 子门。

同一台式机随后通过新增的 PowerShell discovery 参数对当前 `192.168.1.0/24` 做了完整的
有界诊断，报告为
`artifacts/evaluation/field-link-preflight-wrapper-discovery-lan-20260902-r1.json`
（8,540 bytes，SHA-256
`44E30678D5CB354A54FDA8EF4FBFC11376DE289274384FC828BD7B26918EF52C`）。254 台主机中有
4 台返回通用 SSH banner，但 `5760/MAVLink` 和 `554/RTSP` 均未命中；wrapper/verifier
结构门通过，`hardware_link_ready=false` 仍被保留。该发现不会自动选端点或修改网络配置，
接通飞机后必须用实际 Jetson/GR01/相机地址重新跑 `-ProtocolChecks -RequireRoute -RequireReady`。

随后执行的严格单样本复核写入
`artifacts/evaluation/field-link-preflight-current-20260902-continue.json`
（4,397 bytes，SHA-256
`0D3F411D80F01F7D5B4D3CFA63FF1427ABBBFE9A5FC5360BB19E4BDA3B18BA36`）。报告确认当前
源地址仍为 `192.168.1.13/24`，Jetson/GR01/RTSP 三端点、协议检查和期望
`192.168.144.0/24` route 均未通过，而本机 UDP 端口可用；严格退出码为 `1`，
`hardware_link_ready=false`。该证据只更新现场时间基线，不改变任何端点或发布门。

为避免迁移到另一台电脑后 wrapper 仍探测旧地址，本轮另外用显式端点参数做了转发回归，
报告为 `artifacts/evaluation/field-link-preflight-20260902-wrapper-explicit-after-acceptance.json`
（3,712 bytes，SHA-256
`6A5689AF656F6FB51DCD781A7110F296F74A4E38EA1A3492CA47FB3B251CD326`）。报告 `target` 完整
保留命令传入的三个 host 与五个端口；三端点仍未连通，严格 wrapper exit `1`，该文件不替代
绑定在 E7 清单中的现场报告，也不改变 `hardware_link_ready=false`。

## PX4 Mission journal 子门（pre.8）

四种运行模式在调用同一 `Px4MissionTransaction` 时，必须同时使用
`src/multidetect/mission_journal.py` 的 `DurableMissionJournal`/`PersistentPx4MissionTransaction`
适配器。journal 首行固定绑定任务、验证报告、executable Mission、PX4 `MISSION_ITEM_INT` wire
和 RuntimeProfile 的 canonical hash；之后每个上传状态、请求 sequence、ACK、readback、commit
和 start permit 都是 fsync 后才算提交的 hash-chain 记录。

E7 不能仅凭 journal 的最后状态自动判定发布或发出飞控命令。启动前必须重新读取 journal 并
检查 `recovery_required=false`、链完整、绑定与当前运行 profile 一致；掉电留下的非换行尾巴、
乱序/重复状态、旧 ACK、哈希漂移或不完整 readback 会保持 recovery-required。显式
`resume_plan` 只返回补 item、等待 ACK、重新 readback、commit 或人工复核等下一步，
`auto_start_allowed` 永远为 `false`。

机器记录格式为 [`configs/schemas/mission-transaction-journal.schema.json`](../configs/schemas/mission-transaction-journal.schema.json)，
检查入口为 `multidetect-mission-journal <journal.jsonl>`；该子门只能强化事务一致性，不能
把 software-only SIM 证据升级成 HIL、REAL、室外 GNSS 或人工 Fly View 验收。

联合 `SIM` runner 会把 journal 路径同时写入 `vtol_mission_journal`、`hashes.mission_journal`
和 `artifacts.mission_journal`。`scripts/verify_combined_sim_evidence.py` 对这种新 artifact
执行二次完整性检查：重新读取 JSONL、比对字节数与 SHA-256、验证 hash-chain、
`recovery_required=false`、`start_permitted` 终态和完整请求序列；没有该字段的历史 artifact
保持向后兼容，但不会被反向声称已经产生 journal 证据。

## 最新联合 SIM/QGC/Mission v4（2026-09-02，pre.8）

在 QGC `MainWindowHandle` 短暂为零的生命周期窗口加入严格的同 PID 唯一顶层窗口回退后，
当前 portable EXE 完成了 900 帧 Docker PX4/Gazebo/QGC 联合验收。source 为
`artifacts/evaluation/combined-sim-qgc-20260902-hwnd-fallback-v4.json`（249,689 bytes，
SHA-256 `8F90EAB7232A29382451CF99BB5B3CF0E4D44674E9BC4B027C0962120C46D692`），顶层
`gate_passed=true`。独立复核为
`artifacts/evaluation/combined-sim-qgc-20260902T030210Z-77524/combined-evidence-verification.json`
（1,941 bytes，SHA-256
`8913C015977B1C879E07A76A48E9F9D2AD86E0F2F8784F1D81AC68FAE9C71055`），退出码 `0`。

source gate 同时证明集成 SIM、VTOL attach、VTOL Mission、900/900 RTSP/传感器帧、动态
loopback 端口、Docker stop/rm 与 durable Mission journal 全部通过。Mission 观察到
`AUTO.MISSION`、armed、in-air、multicopter、fixed-wing、landed 和 disarmed；Mission item
`0..6` 均产生 reached 记录，飞行持续 `116.078 s`，最远距离 `142.571 m`、相对高度
`26.587 m`。journal 为 13 条 hash-chain 记录，终态 `start_permitted`，独立 verifier 已
重新检查 journal 字节数、SHA-256、链和完整请求序列。

QGC 原生 `videoContent` fixed-wing QQuickItem 抓帧和 transport binding 通过，
`native_qquickitem_video_pixels_proved=true`、
`transport_bound_native_qquickitem_video_surface_proved=true`；原生 toolbar indicator/drawer
机器烟测通过。桌面 GPU 合成 ROI 诊断仍是 `false`，`operator_visual_acceptance=not_claimed`，
所以这项证据仍不等于人工 Fly View 视觉验收。安全边界保持 software-only：
`real_pixhawk_contacted=false`、`real_jetson_contacted=false`、无物理执行器；HIL/REAL、室外
GNSS、现场链路和最终 `v0.3.0` 门继续 fail-closed。v2/v3 历史失败 artifact 不覆盖。

当前 v4 source 已额外封装为
`artifacts/evaluation/release-mode-sim-20260902-pre.8-combined-qgc-v4.json`（562 bytes，
SHA-256 `65B96C6DF3BC66BD77B13D56BE8D5019210B6C52C6638BECA0162980DE9D7CC8`）。独立 verifier
以 `--require-release-mode` 重新绑定 source path/hash，输出
`artifacts/evaluation/combined-sim-qgc-20260902T030210Z-77524/combined-evidence-verification-v4-bound.json`
（2,535 bytes，SHA-256 `5BDAB531DD120645BFE5AF4A8D9816A222EE21B458C020F3EECBA48FE7E1D415`），
`gate_passed=true`。该 envelope 只更新 SIM provenance，不提升 HIL/REAL、现场 GNSS 或人工视觉。

## 当前 E7 预发布清单（2026-09-02，pre.8）

为统一消费当前 v4 source，E7 清单已刷新为
`artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v4.json`（10,852 bytes，
SHA-256 `EAACCA3F40A4B9A6734C550EFFC435A0A13B2F1FC4E08FA12FA0C2F613B66D9F`）。清单绑定
`simulator_combined` 的 v4 source 和 release-mode envelope，并同时绑定 S4 safety、三机型
scenario matrix、PX4 airframe transaction、REPLAY accuracy provenance 及严格现场链路报告。
当前 `simulator_combined.passed=true`、`simulator_safety.passed=true`、
`simulator_scenario_matrix.passed=true`、`px4_airframe_transaction.passed=true`。

固定时间 report-only 与 strict 结果分别为
`artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v4-report.json` 和
`artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v4-strict.json`（均
22,896 bytes，SHA-256
`F7AEC221A979F03D0AED6FC3B5E983B6458A84CE2C89FCBCB9439546CFC22F19`）；report-only 退出 `0`，
strict 退出 `2`。两者都保持 `gate_passed=false`、`release_allowed=false`：REPLAY 精度/部署
覆盖未完成，严格现场链路 `hardware_link_ready=false`，四个模式的人工/硬件/真实控制状态仍
为 `not_claimed`，产品仍是 `prerelease`，最终 release surfaces 也未切换。该清单是
fail-closed provenance，不能替代室外 GNSS、真实 Pixhawk/Jetson、HIL、REAL 或人工 Fly View。

同一台式机随后完成全量 Python 回归：`2400` 项全部通过；parity 越界测试在 symlink 特权
不足时使用 Windows 目录 junction 回退，`ruff`/`compileall` 均退出 `0`。这只补强源码
coverage，不改变上述现场、HIL/REAL、人工视觉和最终版本阻断项。

## 最新现场链路只读复核（2026-09-02，pre.8）

当前台式机的双样本 watch 报告为
`artifacts/evaluation/field-link-preflight-20260902-continue.json`（11,755 bytes，SHA-256
`B779A8D4CCD3FAD11DEDDF03E9E691E0C53552E82381440490814058E14850EB`）。Ethernet 源地址仍为
`192.168.1.13/24`，Jetson `192.168.144.20:22`、GR01 `192.168.144.11:5760`、相机
`192.168.144.108:554` 及期望 route/协议均未就绪；本机 UDP `14580/14581` 可绑定，
`hardware_link_ready=false`。严格单样本报告
`artifacts/evaluation/field-link-preflight-20260902-continue-strict.json`（4,401 bytes，
SHA-256 `2D1F71C74B7B5F13516C29D3C8BF900A3B3F3B54D8EDAB9E0463595749B5D1F6`）在
`-RequireReady` 下 wrapper/verifier 均退出 `1`。这些是现场接线前的 fail-closed provenance，
不关闭真实 GNSS/Pixhawk/执行器、HIL/REAL、人工 Fly View 或最终版本门。

## 最新现场链路复探绑定（2026-09-02，pre.8）

为确认台式机迁移后是否已接入飞机，又执行了 2 个样本、启用 `ProtocolChecks` 的只读
现场复探。报告
`artifacts/evaluation/field-link-preflight-20260902-154000-continue.json`（11,758 bytes，
SHA-256 `1F9A30F4407C1B31435F57155D675CB7BD09EC781BB3734E848D81BB4904F4FC`）显示
Ethernet=`192.168.1.13/24`，期望的 `192.168.144.0/24` route 未命中，Jetson/GR01/相机
端点均超时，`hardware_link_ready=false`。该结果只作为现场诊断 provenance，不选择候选
主机，也不改变端点配置。

该报告已绑定到 E7 清单
`artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v7.json`（10,895 bytes，
SHA-256 `1E16B731CBF6E9252986210266785CE68A505E380065978699A7A421B00A58D2`）。固定时间
`2026-09-02T07:50:00Z` 的 report-only/strict 复核报告为
`artifacts/evaluation/release-gate-pre.8-current-20260902-combined-qgc-v7-report.json` 和
`...-v7-strict.json`（均 23,016 bytes，SHA-256
`A16160554D6A0DFD02414EC67F15E635835B89C7F80D5F0CCF1950C479D04035`），退出码 `0`/`2`；
`simulator_combined` 与 `px4_airframe_transaction` 仍为 `true`，整体
`gate_passed=false`、`release_allowed=false`。

同一结果绑定到
`artifacts/evaluation/multidetect-goal-acceptance-20260902-combined-qgc-v6.json`（39,488 bytes，
SHA-256 `FB24829695396098D3FCB9D37DEC04B22DC0E0E7EABB09F6C75D7C1EA5A3F8E7`）：核心测试
`2421`、QGC custom `66/66`、`software_status=pass`，硬件仍为 `offline_pending`。剩余
ChArUco、舵机安全、真实 `DET→TRK→LCK→TGT` 与 Jetson soak 门需要现场设备。

## 最新联合 SIM source（2026-09-01，pre.8）

当前 `v0.3.0-pre.8` portable EXE 已完成 900 帧 Docker PX4/Gazebo/QGC 联合重跑：
`artifacts/evaluation/combined-sim-qgc-20260901-continue-recovery-v9/combined-evidence.json`
（333,051 bytes，SHA-256
`75265742413146EA78AB88CCBD30B22ACFCC0C5DC7EC48780F4A9D25F649F7FB`），顶层
`gate_passed=true`。source 同时包含 image/CameraInfo/NavSat 连续发布器屏障、RTSP 900/900、
原生 QGC `videoContent`/type-24 transport binding、toolbar drawer、VTOL `MC→FW→MC→DISARM`、
INT-only Mission、hash-chain journal、动态 loopback 端口和 Docker stop/rm 证据；桌面 BitBlt
黑色 ROI 只作为 GPU 合成诊断，不能覆盖已通过的原生 QQuickItem 证据。

要求 release-mode envelope 的独立复核为
`.../combined-evidence-verification.json`（SHA-256
`18FDB7E6E5302229D2244075C26998D8D66E7C7EEBD4FCFB24A6A35111761960`），所有检查为真；对应
envelope 是
`artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v3.json`
（SHA-256 `ACDFB988D7BE669FFEEC4846BDA6F66837B7892CF402DA1D25B01DB84D92A045`）。新的 pre.8
清单/报告为
`artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v5.json`
（SHA-256 `629237034B84498537C07CB7885BCB6F28D20FCD4E8EEA4CAD5D43ACD9618A6A`）/
`...-report.json`（SHA-256
`59C6A45CCC08084683485E39F3D38EFE7869645640C51E7427C41E8ADF5958BD`）。其中
`simulator_combined.passed=true`、`simulator_safety.passed=true`、三机型 Mission transaction
通过；REAL field-link 为 `hardware_link_ready=false`，总 `release_allowed=false`，所以该证据
只关闭 software-only SIM 子门。

最新 goal-acceptance 已重新绑定现场只读预检：
`artifacts/evaluation/multidetect-goal-acceptance-20260901-combined-qgc-v5.json`
（11,200 bytes，SHA-256
`E4690933BC325D1CE2828DDAAC63C65689832AB8E1AFD07AB42AA9D397DE2447`）。对应
`field-link-preflight-20260901-final-continue.json` 的 SHA-256 为
`59F85CEF567B198E12464B306E74DC36177CB5441F997B41A9034F3728025D54`，Jetson/GR01/相机
端点均未连通，故 `hardware_link_ready=false`，不改变 REAL/HIL 和最终发布门。

同一现场窗口还对活动网卡 `192.168.1.13/24` 做了显式限定的只读发现：扫描 254 台主机，命中
4 个 SSH banner，未命中 `5760` MAVLink 或 `554` RTSP 服务。发现报告为
`artifacts/evaluation/field-link-discovery-20260901-continue-v1.json`（8,045 bytes，SHA-256
`03EBA28A99C34420B987ADAB2AC29CED54A5363FE9E4D0C0934AC00FF203CE30`），结构 verifier 使用
`--allow-unready` 退出码为 `0`，但 `hardware_link_ready=false`。该诊断只提供候选主机线索，
不自动选择端点、不改写 Jetson/GR01/相机配置，也不改变 REAL/HIL 发布门。

同一批证据随后重新写入上一轮 pre.8 E7 清单
`artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v5-bound.json`
（10,219 bytes，SHA-256
`C5892E0515D1C6C26199E35CBC80CB405880D5D6C35A0AB045F70EAA7E121393`）。报告
`artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v5-bound-report.json`
（19,841 bytes，SHA-256
`988A88232C325B3FE787FAAA314AC5AFA1AEA0446D031B6DA3FAF2E88AE8EBFD`）确认联合 SIM、S4、
三机型故障矩阵和 PX4 Mission transaction 均通过；严格模式输出
`artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v5-bound-strict.json`
（19,841 bytes，SHA-256
`09212FD35D096BBDAD045DEAD74BA727037A38F74F1100169D9C0CFB143A1B26`）退出码为 `2`，
并准确保留现场/REAL/replay-set、人工视觉/硬件和 prerelease 渠道阻断。

## 当前 v6 现场绑定（2026-09-02，pre.8）

在同一台式机工作树上重新执行总验收后，当前日快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-fault-coverage-v6.json`
（36,169 bytes，SHA-256
`0F59CEC1C31B2B30669598E064F71F217051E86BBD300304BB4B3CA3A4402AE1`）。脚本退出码为 `0`，
核心测试 `2337`、QGC custom `66/66`、软件状态 `pass`；组合 SIM、S4、三机型故障矩阵和
PX4 Mission transaction 子门保持通过。

v6 将现场只读预检更新为
`artifacts/evaluation/field-link-preflight-20260902-continue-v1.json`
（3,038 bytes，SHA-256
`64C06112A967BC0C3BABE904C0588143C182F2A221262A0D97C062E6077119AD`）。报告结构与哈希
复核通过，但当前网卡仍不在飞机 `192.168.144.0/24`，Jetson/GR01/相机端点不可达，故
`hardware_link_ready=false`、`gate_passed=false`、`verification_exit_code=1`；这份证据不
升级室外 GNSS、真实 Pixhawk/执行器、HIL/REAL 或人工 Fly View。

v6 source 已绑定到当前 pre.8 E7 清单
`artifacts/evaluation/release-gate-pre.8-current-20260902-fault-coverage-v6-bound.json`
（10,219 bytes，SHA-256
`48A3D463A05FC30896B8651CB698E88FF242700A56B6CFEDFE83CB34BD78F648`）。report-only 报告为
`...-v6-bound-report.json`（19,798 bytes，SHA-256
`647C0F2EE4514DDBF60F11AC83E14B01C339D39F484E683103C4C44050D3292F`），严格报告为
`...-v6-bound-strict.json`（19,798 bytes，SHA-256
`12A3DDE12D57FF78FF0C5C5B22D4E880A122B2AD18F0230A99FDB293E8B49CB9`）。严格模式退出码 `2`，
`release_allowed=false` 仍由现场链路、REAL/replay-set、人工视觉/硬件和 prerelease 渠道门
阻断；v5 清单和报告继续作为历史 provenance 保存。

为避免 replay-set 在聚合时只显示为缺失，本轮又生成了绑定六分层 replay-set 与 report-only
精度报告的补充清单：
`artifacts/evaluation/release-gate-pre.8-current-20260902-replay-bound-v7.json`
（10,812 bytes，SHA-256
`F4D3831F024D0714934C248425C4B758C6AF557D58BD5F3FA1F10CA488C272C1`）。对应 report-only
报告为
`artifacts/evaluation/release-gate-pre.8-current-20260902-replay-bound-v7-report.json`
（22,599 bytes，SHA-256
`E4C9048133FB17018361A9BE414DDFDCB92F879983B7DD007A81DF513573DD49`），严格复核输出为
`...-v7-strict.json`（22,599 bytes，SHA-256
`F6BA612293DE59CD44CB3ADD29BF7F1FADE0FEA6B36E981FBAABC7BCEB31BA7C`）。该清单现在
`replay_set.present=true`，并保留 `replay_set.passed=false` 的真实原因：部署域覆盖不足、
空基线精度未验证和独立复核未确认；report-only 退出码为 `0`，严格退出码为 `2`，最终
`release_allowed=false` 不变。

随后又对默认 Jetson/GR01/相机端点执行了三样本现场 watch，保存为
`artifacts/evaluation/field-link-preflight-20260902-continue-v2.json`
（12,064 bytes，SHA-256
`F7133D83E89F758A6C4819E71D185F228404DCCAC1A4BC713C7C61C4306F253D`）。三次样本的三个远端
TCP/协议探测均超时，本地 UDP 14580/14581 绑定均成功，因此 `passed_samples=0`、
`failed_samples=3`、`stability_ratio=0.0`、`hardware_link_ready=false`；结构 verifier 在
`--allow-unready` 下退出 `0`，严格 `--require-ready` 仍应退出 `1`。该最新 watch 已替换绑定到
补充 E7 清单
`artifacts/evaluation/release-gate-pre.8-current-20260902-replay-bound-v8.json`
（10,813 bytes，SHA-256
`A3052885BDDF7AB8513441E513A08AC57AE855F39C75E26EA59A5B1B9686BA7A`）。对应 report-only 和
严格报告分别为 `...-v8-report.json`（SHA-256
`F26255A39D99D8E722697E70C214ACEA7545268E1B0110BEDEBA749E47976922`）和
`...-v8-strict.json`（SHA-256
`5D41DF02CB1654DE080BBD6CD97C9E2E55F34760731F4C0AD4684FF4BB7675AF`）；退出码分别为 `0` 和
`2`，现场硬件门、replay-set 精度门和最终 `release_allowed` 继续保持关闭。

为缩短下一次现场恢复时间，预检现在支持可选的本机路由门：
`--require-route --expected-source-cidr <aircraft-cidr>` 会把三个远端服务的本地源地址写入
`route_probes`，并要求它们全部属于显式飞机网段。该检查只调用本机 UDP route selection，
不发 MAVLink、RTSP 或控制数据；源地址不匹配时 readiness 保持 false。当前诊断报告
`artifacts/evaluation/field-link-preflight-20260902-route-v1.json`（4,396 bytes，SHA-256
`21DAD57EA9CF8AD1FA9996F8110193BEA3535E270F7E87C5D7A100A4BA699B13`）显示三个端点都选择
`192.168.1.13`，不在期望的 `192.168.144.0/24`，严格退出码为 `1`；这证实台式机当前没有
飞机网段路由，而不是把 TCP 超时误归因到具体服务。连接物理 Ethernet 后应重新生成同一份
route-enabled watch，再进入 GNSS、RANGE_STATUS、相机与执行器证据门。

该 route-enabled 报告已绑定到当日软件总验收
`artifacts/evaluation/multidetect-goal-acceptance-20260902-route-v1.json`
（SHA-256 `4A11A7ED8F7DE6422640783349343751B8A011192277AD613992531DBC64F8BB`）。总验收
退出码为 `0`，核心测试 `2348`、QGC custom `66/66`、软件状态为 `pass`；这只是更新现场
路由 provenance，`hardware_status=offline_pending`，没有提升 REAL/HIL、室外 GNSS 或人工
Fly View 门。

脚本接入 route 摘要字段后又生成了最新绑定快照
`artifacts/evaluation/multidetect-goal-acceptance-20260902-route-v2.json`
（29,706 bytes，SHA-256
`C7DECB029209C4CA6D05CB74D1B951E89811424C884FA0F051D1F5378E87BEE4`）。该快照明确记录
`route_checks_requested=true`、`route_checks_passed=false`、
`route_source_cidr=192.168.144.0/24`，并保留 `hardware_status=offline_pending`；旧 v1 快照
继续作为历史 provenance 保存。

E7 的 `real_field_link` 记录也接受并回写这三个 route 字段；当最终 REAL 报告通过
`--real-field-link-preflight` 绑定时，发布 manifest 会同时记录期望 CIDR 和 route gate
结果，并在每次重算时与现场报告逐字段比对。没有 route 字段的旧 prerelease manifest 仍可
读取，但新的 route-enabled 报告不会被降级成只有 TCP 可达性的摘要；切换到最终 `release`
通道时，`route_checks_requested=true`、`route_checks_passed=true` 和非空
`route_source_cidr` 变为强制条件。

本轮已用当前 route-enabled 现场报告实际生成一份端到端绑定清单，作为 writer/evaluator/schema
的可复核样本：`artifacts/evaluation/release-gate-pre.8-current-20260902-route-bound.json`
（10,924 bytes，SHA-256
`E9DCE9776A50C6957479D26992576146A3DA1E9378CF976892FB11F85B3F1449`）。报告模式输出为
`artifacts/evaluation/release-gate-pre.8-current-20260902-route-bound-report.json`
（22,755 bytes，SHA-256
`A174F8A999777737C5737C5B91E4908A08B12F7741CDFC5A52405AA5EA96A1AD`），严格输出为
`artifacts/evaluation/release-gate-pre.8-current-20260902-route-bound-strict.json`
（22,755 bytes，SHA-256
`73251284191B662184E4E9BF6E3BB8B18B64DE9968D7FB3B91E458C0B7B128FD`）。报告模式退出码为
`0`，严格模式退出码为 `2`；聚合结果明确为 `gate_passed=false`、`release_allowed=false`、
`real_field_link.passed=false`，并保留
`route_checks_requested=true`、`route_checks_passed=false`、
`route_source_cidr=192.168.144.0/24`。三个端点实际选择的 `192.168.1.13` 不属于期望飞机网段，
所以这份清单证明的是 route provenance 已进入 E7 且失败时保持 fail-closed，不是硬件/REAL 通过。

为消除报告时间字段造成的哈希漂移，本轮又用固定
`--evaluated-at-utc 2026-09-02T00:00:00Z` 生成了可归档副本：
`artifacts/evaluation/release-gate-pre.8-current-20260902-route-bound-reproducible-report.json`
（22,755 bytes，SHA-256
`765F2752F9394953D799D23BC06B3323A400710A7CC06FBC94201280C11DCA9A`）。使用同一清单、
同一证据和同一时间戳重复执行，报告字节与 SHA-256 均一致；它仍明确保持
`gate_passed=false`、`release_allowed=false`，不会把离线现场提升为 REAL。

随后用 2026-09-02 的三样本 route/protocol watch 和当日总验收产物重新生成了当前
pre.8 绑定清单：
`artifacts/evaluation/release-gate-pre.8-current-20260902-route-continue.json`
（10,925 bytes，SHA-256
`4F389AEE91ADEEB79F4CDC9E200643F4DDB5F52D9E1A770C8D84148F9EECA9F2`）。以固定
`--evaluated-at-utc 2026-09-02T00:00:00Z` 运行时，report-only 与严格输出均为
22,816 bytes、SHA-256
`3A4DD9D10BDB34FAD12EF2878633DC956D315FFB4784D0B25FDE40507419EC35`；退出码分别为
`0` 和 `2`。SIM safety、三机型 scenario matrix、PX4 transaction 和 external-SIM
revalidation 子门保持通过，现场 route/GNSS、replay-set 精度、人工视觉、HIL/REAL 和
prerelease channel 仍按设计阻断最终发布。

## 当前总验收中的 HIL 合约节点（2026-09-02，pre.8）

`scripts/run_goal_acceptance.ps1` 现在每次总验收都会直接运行 Mode-2 payload 与 Mode-3
approach 两个 HIL-contract runner，并把它们的路径、SHA-256、事件和控制边界写入顶层
`hil_contract_acceptance`。这消除了“单独 runner 通过、总验收未绑定”的证据断层；节点只表示
软件/HIL 合约闭环，不能把 advisory 结果解释成真实台架、执行器或室外飞行通过。

本轮总验收为
`artifacts/evaluation/goal-acceptance-20260902-030411-hil-contract-route-v1.json`
（31,989 bytes，SHA-256
`BDDF745A9586E67242DCDBD242AB2BFAF6CE86CDEB21C30F03BEBC03E91B737D`），脚本退出码 `0`，
核心测试 `2352`、QGC custom `66/66`、`software_status=pass`，且
`hil_contract_acceptance.all_contracts_passed=true`。绑定的 Mode-2 证据为
`...-mode2-payload.json`（1,195 bytes，SHA-256
`8948429D6884D32AED11D47296A3D49055A98049831FC90ACC095C9487777997`），必须保持
`flight_control_enabled=false`、`physical_release_enabled=false`、
`real_payload_interface_present=false`；绑定的 Mode-3 证据为
`...-mode3-approach.json`（2,513 bytes，SHA-256
`F9A1351A9C777E4F29229DD8BF9FD476FC68FD6080ECC60AA696D7447A7364D5`），必须保持
`advisory_only=true`、`flight_control_enabled=false`、`physical_release_enabled=false`、
`real_actuator_interface_present=false`。

该总验收同时保留现场 route fail-closed 事实：三个远端服务的被选源地址仍是
`192.168.1.13`，不属于期望的 `192.168.144.0/24`，因此
`hardware_link_ready=false`、`hardware_status=offline_pending`。HIL 合约节点、SIM safety、
三机型故障矩阵、PX4 Mission transaction 和 external-SIM revalidation 子门通过，不能替代
真实 Jetson/相机/GNSS/Pixhawk、物理执行器、HIL 台架或人工 Fly View 验收；最终 `v0.3.0`
仍保持未发布。

随后在加入 `prepare_field_link.ps1` 和其回归测试后再次执行完整聚合。最新快照为
`artifacts/evaluation/goal-acceptance-20260902-031536-field-link-tool-v1.json`
（31,965 bytes，SHA-256
`08D0CEF2EEEDED68B79E28F7E40BA09337987D443DBDD2F5E50A9E23DFC23EF5`），脚本退出码 `0`，
核心测试 `2353`、QGC custom `66/66`、`software_status=pass`；
`hil_contract_acceptance.all_contracts_passed=true` 仍为真，Mode-2/Mode-3 绑定哈希分别为
`0E5BE4437D25B9BA97D4B21C6FC76CDDA506CA727C1E956032C51C3912CA2FD4` 和
`B731E41D9E9A9C23AB11FD912F80544E5158029F393340BA84E8D71BE7C65248`。
这次重跑没有改变现场 route 结果：三个端点仍选择 `192.168.1.13`，不属于
`192.168.144.0/24`，所以 `hardware_link_ready=false`、`hardware_status=offline_pending`；
HIL 合约、SIM safety、三机型矩阵、PX4 Mission transaction 和 external-SIM 子门仍为通过，
最终发布门继续保持关闭。

同一批 source 已重新生成 HIL release-mode envelope
`artifacts/evaluation/release-mode-hil-20260902-031536-field-link-tool-v1.json`（622 bytes，
SHA-256 `1171756830158D276BEF0EE9FD8E4354F394A174C1CAB5589637D19991F6BE3A`），并换绑到
`artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-tool-v1.json`（10,957 bytes，
SHA-256 `428A2E78B25C0FDFB9EC247D6DD9A4882077DF6A1A73A0D21DB6D2090CE442A8`）。使用固定时间
`2026-09-02T00:00:00Z` 生成的 report-only 与 strict 输出均为 22,852 bytes、SHA-256
`A0D4073B90166D34DD1515C79B43E68F57D5CA2D14B46D58A2C1FA7E401EAE24`，退出码分别为 `0` 和
`2`；HIL source gate、SIM safety/矩阵、PX4 Mission transaction 仍为软件子门通过，
但 HIL 视觉/硬件状态仍是 `not_claimed`，现场 route、REAL、replay-set 精度和 prerelease
渠道继续阻断 `release_allowed`。

本轮还补了一个可回滚的现场网卡配置入口：
`scripts/prepare_field_link.ps1` 的 `-VerifyOnly` 只读输出当前网卡、地址和
`192.168.144.0/24` 路由；提升后的默认 apply 只增加 `192.168.144.250/24` 二级地址，
不设置网关/DNS，也不发送 MAVLink、RTSP 或控制数据；`-Remove` 精确删除该地址。
当前台式机执行 apply 时因非管理员令牌返回 `Access denied`，所以没有改变系统网络状态，
route watch 与 E7 仍保持 fail-closed。接好飞机 Ethernet 后应在管理员 PowerShell 执行：

~~~powershell
Set-Location D:\Projects\Multi-Detect
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\prepare_field_link.ps1 -IpAddress 192.168.144.250
~~~

普通 PowerShell 也可显式加 `-Elevate` 触发 UAC；脚本等待管理员子进程结束并传回相同退出码：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\prepare_field_link.ps1 `
  -IpAddress 192.168.144.250 -Elevate
~~~

然后立即用 `run_field_link_preflight.py --protocol-checks --require-route
--expected-source-cidr 192.168.144.0/24 --samples 3 --interval-s 0.5 --require-all` 重新生成
带 route provenance 的报告；若现场地址冲突，先用 `-IpAddress` 选择确认空闲的同网段地址，
不要覆盖原有 `192.168.1.13/24`。

本轮还将 Windows QGC 精确客户端抓帧的前台/遮挡检查改成最多 5 次有界稳定窗口，并要求
Gazebo 三个必需传感器发布器连续两次存在；持续遮挡仍 fail-closed。attached VTOL 在 ACK 后
等待新鲜 HEARTBEAT，Mission evaluator 使用 `resume_plan()`，对应修复均已由联合 runner 和
源码回归覆盖。

## 当前软件会话契约证据（2026-09-02，pre.8）

### UAC 提权入口与最新总验收刷新

新增 `scripts/prepare_field_link.ps1 -Elevate` 后重新执行总验收。快照为
`artifacts/evaluation/goal-acceptance-20260902-033125-field-link-uac-v3.json`
（31,491 bytes，SHA-256
`1292D7CB4A059DA47CA8BCCFC723855284998ED2D05627D84571C9334F627030`），退出码 `0`，核心测试
`2353`、QGC custom `66/66`、`software_status=pass`、`hardware_status=offline_pending`，
并且 `hil_contract_acceptance.all_contracts_passed=true`。HIL source envelope 为
`artifacts/evaluation/release-mode-hil-20260902-field-link-uac-v3.json`
（539 bytes，SHA-256
`363954C63B419A1F479A4452A6617EDD708D959E3883E235A396169BA2648F29`）。

对应 pre.8 E7 清单为
`artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-uac-v3.json`
（10,888 bytes，SHA-256
`2AB23A691DD3B7B54899760C01A2445D207F5310693EA4382DB7A53E0C08B5D3`）；固定时间
`2026-09-02T00:00:00Z` 的 report-only/strict 报告均为 22,782 bytes、SHA-256
`672533CA780DED8A37183FC8D3CEC267AA3CA586D9B3F3134252FA911DD28973`，退出码分别为 `0` 和 `2`。
UAC 入口只负责把同一脚本和参数交给管理员 PowerShell，不改变默认网关/DNS，不发送飞控或
RTSP 数据；现场 route、室外 GNSS、REAL/HIL 物理和人工视觉门继续保持 fail-closed。

随后加入现场 `TGT` 状态证据门并重新跑完整聚合：
`artifacts/evaluation/goal-acceptance-20260902-target-state-evidence-v1.json`
（31,511 bytes，SHA-256
`BA96AA7ACED29AEFE8D9E61C4C56BBD876558BE0715877E53110BB3EAC0235FC`），核心测试收集 `2358`、
QGC custom `66/66`，脚本退出码 `0`，`software_status=pass`、`hardware_status=offline_pending`。
strict TGT gate 的 HIL envelope 为
`artifacts/evaluation/release-mode-hil-20260902-target-state-evidence-v1.json`
（444 bytes，SHA-256
`B06C112E84A775458136B802224AB1CF73AA0CE247382F11217DB1B759D0FFFF`）。绑定后的 E7 清单为
`artifacts/evaluation/release-gate-pre.8-current-20260902-target-state-evidence-v1.json`
（10,895 bytes，SHA-256
`AC50DC5E5114FB54EECB9566E27CE8FC91F92806D92F79C29722A74446EB668A`）；固定时间 report-only
报告 22,796 bytes、SHA-256
`7A87E62413805443CD12FE6692448A6B40E937BAEB7AB5130275DC33BE9CBC4D`，退出码 `0`，strict 同字节
报告退出码 `2`。新增门只强化现场目标态证据，不把离线 fixture 或当前无链路状态升级为真实
GNSS/硬件通过，最终 `release_allowed=false` 保持不变。

同轮对当前 `192.168.1.0/24` 做了有界协议发现，证据为
`artifacts/evaluation/field-link-discovery-20260902-033839-desktop-uac-protocol.json`
（8,437 bytes，SHA-256
`C5BB3A87128F452F8C9A7269CCDE44376AF77889B90AD5373EED61EED9A1F2E5`）：4 个主机返回 SSH
banner，但 `5760/MAVLink` 和 `554/RTSP` 均为 0。发现结果只作为诊断，不能把通用 SSH 主机
提升为 Jetson，也不能替换显式现场端点或关闭硬件门。

为把最近的取消/重捕获回归固定成可重放证据，本轮从现有字段 fixture 生成了独立的
software-contract capture：
`artifacts/evaluation/field-session-software-contract-20260902-cancel-reacquire.jsonl`
（1,866 bytes，SHA-256
`AB10F56D0E6D6E7D1B35810A89498D1923B5AF74FB5B28748984D888493BB892`）。记录序列严格为
`DET → TRK → LCK → TGT → cancel_trk → DET → TRK`，覆盖一次点击取消、目标框清除和重新捕获。
对应捕获报告为
`artifacts/evaluation/field-session-software-contract-20260902-cancel-reacquire.capture.json`
（5,110 bytes，SHA-256
`523A12886FE7A816A6271C370B256F754D8B490AE90ACB8BF78578100F88C1B1`）。

报告的 source/output hash、JSONL 校验和、状态序列及 GPS/RANGE 质量门均已由
`scripts/verify_field_session_capture.py --require-gate` 重算通过（退出码 `0`）。这份文件只
证明软件契约和回放路径，不声明真实 Jetson、相机、室外 GNSS、Pixhawk、执行器、HIL、REAL 或
人工 Fly View 已通过；真实现场数据接入后必须生成新的现场绑定报告并重新运行 E7 聚合器。

现场真实采集应同时启用 `--require-target-state-evidence`。该开关将每条传感器样本绑定到最近的
`DET/TRK/LCK/TGT` 上下文，并要求 `TGT` 上下文内同时有新鲜合格 GPS 与 `RANGE_STATUS`；取消事件
清除上下文，复核器从 capture report 中读取并重复同一开关。这样，`DET` 阶段合格但到 `TGT`
阶段已经失鲜的数据会直接关闭现场会话门，而不会被当作目标落图证据。

该 capture report 现在还能通过 `--real-target-state-evidence-report` 写入 E7 清单。清单绑定
报告字节数和 SHA-256、完整状态路径以及 `TGT` 内两类传感器的合格记录索引；`release_gate.py`
每次评估都重新运行 `verify_field_session_capture`，任何报告替换、开关关闭、状态摘要漂移或
复核失败都会关闭该绑定。最终 `v0.3.0` 要求这份严格目标态会话证据，同时仍要求 route-bound
REAL 链路和独立目标坐标精度报告；软件 fixture 可以作为 pre.8 诊断，但不能替代现场证据。

本轮用取消/重捕获 fixture 生成的绑定清单为
`artifacts/evaluation/release-gate-pre.8-current-20260902-target-state-evidence-v1-bound.json`
（SHA-256 `6AB1053D1DE5EE49BC81BE593210918186650990B28493F190DD33E7DC8A33DC`）。其中
`real_target_state_evidence.passed=true`，但整份清单的 report-only 结果仍为
`release_allowed=false`；该文件只验证 E7 绑定和重算路径，不提升 replay、REAL/HIL、室外 GNSS
或人工 Fly View 的状态。

同一轮还对当前六分层 SIM replay-set 做了 report-only 重算，输出
`artifacts/evaluation/replay-set-accuracy-20260902-pre.8-report-only.json`
（9,043 bytes，SHA-256
`3DAAECBC63AC5BB512B60D9817DF5B1CFF0F0FA8995930D8749583A8C242B319`）。报告确认清单结构、
帧覆盖、预测完整性和指标计算路径有效，但 `deployment_domain_evidence_complete=false`、
`reviewed=false`、`accuracy_validated=false`，失败原因明确为部署域证据不足和独立精度复核未确认。
该结果保留 replay-set 发布门的 fail-closed 语义；空基线不能被写成检测精度通过。

最新总验收也已使用上述三样本现场 watch 重新生成：
`artifacts/evaluation/multidetect-goal-acceptance-20260902-field-watch-v7.json`
（36,154 bytes，SHA-256
`B240A5999B64C3EEE582EB74CDBCE75A90012FDD0BD470F91CFF30AFCBA2077A`）。该快照脚本退出码为
`0`，核心测试收集 `2337`、QGC custom `66`、软件状态 `pass`，并明确绑定
`field-link-preflight-20260902-continue-v2.json`（`hardware_link_ready=false`，预检验证退出码
`1`）。快照中的剩余现场门为 ChArUco 20+ 视角标定、断桨舵机方向/限位/失锁回退、真实
`DET → TRK → LCK → TGT` 以及 Jetson latency/temperature/RSS/reconnect soak；因此它只刷新
当前日 software/field provenance，不改变 HIL/REAL 或最终 `v0.3.0` 发布状态。

随后在当前台式机工作树上又完成了一次 route-aware 继续验收：
`artifacts/evaluation/multidetect-goal-acceptance-20260902-desktop-continue.json`
（31,412 bytes，SHA-256
`9E7347F288151F53245DD103242993183C38F6ED363079F4C53C63A3EC2A9E66`）。该快照收集 2,366
项核心测试、QGC custom `66/66`，`software_status=pass`，并绑定
`field-link-preflight-20260902-desktop-continue.json`（16,806 bytes，SHA-256
`98C6039B2E6988E066DA82C549530AB25C160DE6EFDAA0FEC50F21396D2A68BC`）。当前 Ethernet
源地址仍为 `192.168.1.13/24`，未形成 `192.168.144.0/24` route，Jetson/GR01/RTSP
均超时，`hardware_status=offline_pending`；这次只更新桌面 provenance，不关闭 REAL/HIL 门。
该现场报告随后重新绑定到
`artifacts/evaluation/release-gate-pre.8-current-20260902-desktop-continue.json`
（11,459 bytes，SHA-256
`29729C9201B2A755C4E4017B11958944B5DCB3C4AEEE73CB660F78468370B327`），并由
`check_release_gate.py --report-only` 生成
`artifacts/evaluation/release-gate-pre.8-current-20260902-desktop-continue-report.json`
（25,312 bytes，SHA-256
`AC6A50DDAD83783FE3FED5F0984F5A9E04C306B9C510CB6782AF93A9B1B6E19E`）。聚合结果仍为
`gate_passed=false` / `release_allowed=false`；更新的意义是 E7 现在 hash-bind 当前现场
报告，硬件未接入的 fail-closed 结论保持不变。

## 当前 3-D SIM 与三机型交互控制子门（2026-09-02，pre.8）

本轮新增的 3-D 控制证据为
`artifacts/evaluation/simulator-3d-control-20260902-pre.8-continue-v1.json`
（82,463 bytes，SHA-256
`56099CE79D65DF7D240BFC633E3479EA848053E4665F62AA0B6157F2509A82AC`）。该文件绑定
`river_rescue_vtol_001` 场景 manifest、Gazebo world/model 资产和 WGS-84 原点，
`gate_passed=true`、`deterministic_gate_passed=true`，并验证 manual QGC、AI TRACK、
manual takeover、AI resume、watchdog neutral/failsafe 和 QGC-only loopback。它是
`deterministic_protocol_asset_preflight` 子门；`external_runtime_gate_passed=false`、
`external_3d_runtime_acceptance=not_claimed`，因为本次没有启动外部 PX4/Gazebo 进程，
也没有进行人工视觉或硬件验收。

对应的 native QGC 交互证据为
`artifacts/evaluation/simulator-interactive-20260902-pre.8-continue-v1.json`
（111,844 bytes，SHA-256
`24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE`）。`--mode all`
覆盖 `vtol`、`fixed-wing`、`multirotor`，三种机型均通过 manual-priority、AI resume、
watchdog 和共享 authority/adapter/flight-model 检查。两份 artifact 的外部 PX4/Gazebo、
视觉、Pixhawk/Jetson/相机、室外 GNSS、HIL/REAL 与实飞字段均保持 `not_claimed`，所以只
关闭 software/SIM 交互子门，不改变现场和最终发布门。

同日又按 `v0.3.0-pre.8` 源码刷新了 `-continue-v2` 证据，供当前 provenance 引用：

| 子门 | artifact | bytes | SHA-256 | 结果 |
|---|---|---:|---|---|
| native QGC 交互（三机型） | `simulator-interactive-20260902-pre.8-continue-v2.json` | 111,844 | `24B57DAA77551CBE9970D95A7A93C5AC6209269F044285F5DD7B551467B313BE` | `gate_passed=true` |
| VTOL 3-D 控制 | `simulator-3d-control-20260902-pre.8-continue-v2.json` | 82,435 | `1D4F90E0C631BCB42A7F9DF5BD6F9C4B7468CD37AAA891BF96AF8CF11DD64177` | `gate_passed=true`, `deterministic_gate_passed=true` |
| fixed-wing 3-D 控制 | `simulator-3d-control-20260902-fixed-wing-continue-v2.json` | 83,126 | `FB879AE4F627474B24D3397739A6E3ECC60F92891CCF671CC846414D02697982` | `gate_passed=true`, `deterministic_gate_passed=true` |
| multirotor 3-D 控制 | `simulator-3d-control-20260902-multirotor-continue-v2.json` | 83,089 | `84419C67A853D6467A900CC76154EE1699052123B456C0C1D0BC66B006ABFCFE` | `gate_passed=true`, `deterministic_gate_passed=true` |

三份 3-D artifact 的 `external_runtime_gate_passed=false`，外部 PX4/Gazebo、3-D 视觉、
Pixhawk/Jetson/相机、室外 GNSS、HIL/REAL 和实飞字段继续是 `not_claimed`。这组 v2 只关闭
deterministic software/SIM 子门，不改变现场或最终 `v0.3.0` 发布门。

独立回归命令如下（退出码 `0`）：

```powershell
Set-Location D:\\Projects\\Multi-Detect
$python = '.\\ground-control\\MultiDetectGCS\\.venv\\Scripts\\python.exe'
& $python scripts\\run_simulator_3d_control_acceptance.py `
  --out artifacts\\evaluation\\simulator-3d-control-20260902-pre.8-continue-v1.json
& $python scripts\\run_simulator_interactive_acceptance.py `
  --mode all `
  --out artifacts\\evaluation\\simulator-interactive-20260902-pre.8-continue-v1.json
& $python -m pytest tests\\test_simulator_3d_control_acceptance.py `
  tests\\test_simulator_interactive_acceptance.py `
  tests\\test_simulator_interactive_router.py `
  tests\\test_simulator_external_evidence.py -q --disable-warnings
```

### 当前三机型 3-D 控制绑定（2026-09-02，pre.8）

为避免只在单个 VTOL 快照上看到绿色结果，E7 manifest 现在可绑定三份独立的 3-D
控制证据。当前绑定文件为
`artifacts/evaluation/release-gate-pre.8-current-20260902-3d-bound.json`（13,373 bytes，
SHA-256 `967F8C5CFE4DBF16EAFF88503BBF18C497BAEA4ECE8190DF2D5602F1D7B8D206`），其中：

| 机型 | source bytes | source SHA-256 |
|---|---:|---|
| VTOL | 83,212 | `3733B2E02F4751B5E9262451EC6C7BF73FC5EDEC94E57B6F3F4755A603ADB1F7` |
| fixed-wing | 83,903 | `336CFABAB3CC88E4419F5FD3744890D4F789F3EAFA1C571D063F98515881C6EE` |
| multirotor | 83,866 | `2F99FBCDE6457EE634EA65FB5D58988B90628CF38E7A74C83EB3CEA5D470EDE0` |

`src/multidetect/simulator/three_d_control.py` 是独立复核边界：它重新打开场景 manifest、
Gazebo world/model、stack-plan、PX4 launch preflight、manual/AI/watchdog/QGC-only contract，
并校验每份 source 的 SHA-256 和字节数。绑定必须恰好包含
`fixed-wing`、`multirotor`、`vtol` 各一次。脚本入口为
`scripts/verify_simulator_3d_control_evidence.py`：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src'
python scripts\verify_simulator_3d_control_evidence.py --bind `
  --expected-release-label 0.3.0-pre.8 `
  --manifest-directory artifacts\evaluation `
  artifacts\evaluation\simulator-3d-control-acceptance-20260902-current-pre8.json `
  artifacts\evaluation\simulator-3d-control-acceptance-20260902-current-fixed-wing-pre8.json `
  artifacts\evaluation\simulator-3d-control-acceptance-20260902-current-multirotor-pre8.json
```

`release_gate.py` 每次评估都会重新哈希并复核这三份 source；最终版本还要求该绑定存在且
通过。当前预发布聚合仍为 `gate_passed=false` / `release_allowed=false`，因为 REPLAY
精度、现场链路、人工 Fly View、HIL/REAL 和硬件门没有完成。三份 artifact 的
`external_runtime_gate_passed=false`、`external_runtime_acceptance=not_claimed` 是强制的，
不能把 deterministic asset/control 结果写成外部 PX4/Gazebo、Pixhawk 或室外 GNSS 已验收。

`scripts/run_goal_acceptance.ps1` 的默认流程现在会重新生成 VTOL、fixed-wing、multirotor
三份 source，再调用同一绑定验证器；可用 `-Simulator3dVtolOutput`、
`-Simulator3dFixedWingOutput`、`-Simulator3dMultirotorOutput` 指定输出，或用
`-RequireSimulator3dControlEvidence` 将绑定设为硬前置。

完整 goal-acceptance 的实际重跑结果为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-3d-binding.json`（41,392 bytes，
SHA-256 `57F9B416A8E4FF4EC20D395F905A87DAA4290304B930EF1DA448CFBAEE8197E9`），
`core_tests_collected=2456`、QGC custom `66`、`software_status=pass`，并记录
`simulator_3d_control_acceptance.verified=true`。同批 latest source 生成的绑定清单为
`artifacts/evaluation/release-gate-pre.8-current-20260902-3d-bound-rerun.json`（13,328 bytes，
SHA-256 `BA87F55307793069A04BC941E3F62586AE64476EE5BD86A27C1B52AF8CA97187`），
`simulator_3d_control.passed=true`；总门仍为 `gate_passed=false` / `release_allowed=false`。

随后把最新台式机现场只读预检和三机型 source 文件名绑定进一次独立总验收：现场报告
`../artifacts/evaluation/field-link-preflight-20260902-final-continuation.json`（3,713
bytes，SHA-256
`476AB14F0E48887A33AA4107D86A6782F13B8A7B4334B95954681F99A3A48E52`）被 verifier 重新
打开并判定 `hardware_link_ready=false`；Ethernet 为 `192.168.1.13/24`，期望机载网段为
`192.168.144.0/24`，Jetson/GR01/RTSP 端点均不可达，本地 UDP 端口可用。总验收快照为
`../artifacts/evaluation/multidetect-goal-acceptance-20260902-final-continuation.json`
（41,681 bytes，SHA-256
`6F6388CA393F49FEADC6B95693A0CB7AC93959A2AA9E17236B8BC7DC7DF3AB9C`），核心测试 2,456、
QGC custom 66/66、软件门通过、硬件 `offline_pending`，三机型 binding 强制验证退出码为
0。该批 source 指纹为：VTOL 83,240 bytes / `50B8E18B1A7B7D85CF4D37AF48BD5DB1B51534C57F709B864E2FB8827A1BEF5F`，
fixed-wing 83,931 bytes / `715A3437101846271A901CC90140DAC9969D459361D7FEE6EF438F75DE945208`，
multirotor 83,894 bytes / `98B1971CC17F9EF3190EB83759329C1BFDDE1EF64F2E2383B9ED4DA036641E19`。
三份 source 的独立绑定报告为
`../artifacts/evaluation/simulator-3d-control-binding-final-continuation.json`（2,278 bytes，
SHA-256 `262C659B1860FAA2098AC48B211588A2CE523604A4C45A465E7F530C6F3F0C96`），其中
`all_airframes`、`records_hash_bound`、`all_gate_passed` 和
`external_runtime_not_claimed` 均为 `true`。
该绑定只更新可重复的 deterministic 3-D source provenance；外部运行时、人工视觉、室外
GNSS、Pixhawk/Jetson/相机、HIL/REAL 仍保持 `not_claimed`，最终发布门不变。

### 台式机 Docker 直接重跑绑定（2026-09-02，pre.8）

Docker Desktop 实际可用（`C:\\Program Files\\Docker\\Docker\\resources\\bin\\docker.exe`，server `28.4.0`）。本轮用 pinned
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
重新执行 VTOL、fixed-wing、multirotor 的 external runtime 与
`--exercise-arm --exercise-target` live-AI 路径。六份 source 均通过 runtime、qualified
GPS/heartbeat、AI `TRACK` 和容器清理；文件级 join 为
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-continue-v1.json`
（5,426 bytes，SHA-256 `1A3D7F99BC0E6C21E68BDFA57B9DFA77856C030E3DC3DE406CB4ED3A78FD63B5`，
`gate_passed=true`）。三组 runtime/AI source 的 SHA-256 分别记录在该 join 的 `vehicles`
节点，并绑定生成时的 `configs/release-parity.json`（SHA-256
`6D0CDBD3D376FBE1AF46A51C721AF3785528CA6BF22016DCD4216979769D7FDF`，该 join 生成时的历史
parity）。这只关闭 headless
external-SIM 与 truth-free AI/target policy 子门；`operator_visual_acceptance`、真实硬件、
室外 GNSS、HIL/REAL 和实飞仍为 `not_claimed`，不改变最终 `release_allowed=false`。

该 join 已接入总验收并通过同一 revalidator：
`artifacts/evaluation/multidetect-goal-acceptance-20260902-docker-ai-continue-v1.json`
（31,778 bytes，SHA-256 `CDDD0C7E6F2DFA7C4160AC88DA9CC393C372371F5B3D1204D8C30C5666CFFB28`）记录
`simulator_external_evidence.provided=true`、`verified=true`、`source_recomputed=true`、
`gate_passed=true`，核心测试 `2,397`、QGC custom `66/66`、软件状态 `pass`；对应的
revalidation 文件为
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-pre.8-continue-v1.json`
（543 bytes，SHA-256 `50262ECE3F26907ED69F038134888120089CB6F63CB9B9F5931F8D5FC2C7F12F`）。
总发布门仍保持 `release_allowed=false`，因为现场链路、室外 GNSS、人工视觉、HIL/REAL
和 REPLAY 精度门尚未完成。

## 当前 Docker external PX4/Gazebo + live-AI 三机型子门（2026-09-02，pre.8）

本轮在固定 digest 镜像
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
上重新执行 VTOL、fixed-wing、multirotor 的 disposable PX4/Gazebo runtime，并在每个
场景上启用 live AI ARM/target exercise。六份 source evidence 均通过 runtime/preflight、
qualified telemetry 和 cleanup；AI source 均记录 `ai_steps=2`、新鲜 armed heartbeat、
`target_exercise.accepted=true`、`decision.action=track`，且 `truth_free=true`、
`hardware_control_disabled=true`。

当前 source 文件与 SHA-256：

| 机型 | runtime evidence | AI evidence |
|---|---|---|
| VTOL | `simulator-docker-acceptance-20260902-pre.8-desktop-v2.json` · `E9A95A852BBB91171E88281616A896AC8277D151C57B81DBCBE0E51A8B2802B3` | `simulator-docker-ai-acceptance-20260902-pre.8-desktop-v1.json` · `1C12C3699348C18AEF29321975511F08C281A8C26D827EFB5992D88ECB7E04F0` |
| fixed-wing | `simulator-docker-acceptance-20260902-pre.8-fixed-wing-v1.json` · `B5D46C2C8820CF5BFC552015AD8522AE5A53AF272749A882693D017B41BF9385` | `simulator-docker-ai-acceptance-20260902-pre.8-fixed-wing-v1.json` · `1A9A62C2089D4866928036AF2BCCA38A567E36156823AFD9794B4F310A2B88F7` |
| multirotor | `simulator-docker-acceptance-20260902-pre.8-multirotor-v1.json` · `7E2338F0CA45E7886A5294DEE1E612C0B1315C8A5F6A0660A113C4E8AB775F26` | `simulator-docker-ai-acceptance-20260902-pre.8-multirotor-v1.json` · `F7E1466DFE29B20912EF4FD57DE5D3AF358B118D3D9F0EC741CB27C478F289B9` |

六份 source 经 `scripts/verify_simulator_external_evidence.py` 按同一
`configs/release-parity.json` 做文件级 join，输出
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260902-pre.8-v2.json`
（5,383 bytes，SHA-256
`656531912FC0FCF8AF975469CBB0C340136CF4D8515BECE5BFC1827858BDDC35`），
`gate_passed=true`。join 重新计算 source 哈希并绑定场景 manifest、镜像 digest、pre.8
版本、GPS 质量、truth-free 感知、AI/target policy、armed heartbeat、硬件控制边界和
清理状态；`visual_accuracy`、`operator_visual_acceptance`、`real_hardware` 仍为
`not_claimed`。

本轮修复的工程缺口是动态 telemetry 端口：`src/multidetect/simulator/stack.py` 现在对
每个 job 的 loopback telemetry 端口显式记录 canonical PX4 `14541` 到选定端口的
`sensor_port_bindings`，并继续拒绝无关 MAVLink 端口。`tests/test_simulator_stack.py`
覆盖重映射和非法端口回归，确保并行 job 不会因固定 `14541` 互相碰撞，也不会放宽来源校验。

这只关闭 SIM external runtime 与 AI/target policy 机器子门。它不关闭 native wrapper-backed
PX4/Gazebo、QGC 人工 Fly View、真实 Pixhawk/Jetson/相机、室外 GNSS、HIL、REAL 或实飞。

复核命令：

```powershell
Set-Location D:\\Projects\\Multi-Detect
$python = '.\\ground-control\\MultiDetectGCS\\.venv\\Scripts\\python.exe'
& $python scripts\\verify_simulator_external_evidence.py `
  --vtol-runtime artifacts\\evaluation\\simulator-docker-acceptance-20260902-pre.8-desktop-v2.json `
  --vtol-ai artifacts\\evaluation\\simulator-docker-ai-acceptance-20260902-pre.8-desktop-v1.json `
  --fixed-wing-runtime artifacts\\evaluation\\simulator-docker-acceptance-20260902-pre.8-fixed-wing-v1.json `
  --fixed-wing-ai artifacts\\evaluation\\simulator-docker-ai-acceptance-20260902-pre.8-fixed-wing-v1.json `
  --multirotor-runtime artifacts\\evaluation\\simulator-docker-acceptance-20260902-pre.8-multirotor-v1.json `
  --multirotor-ai artifacts\\evaluation\\simulator-docker-ai-acceptance-20260902-pre.8-multirotor-v1.json `
  --parity-json configs\\release-parity.json `
  --out artifacts\\evaluation\\simulator-external-runtime-ai-evidence-20260902-pre.8-v2.json
```

对已经生成的 join 继续执行 `--join` 二次验证，会读取 join 中保存的六个 source path 和
parity path，重新计算 source 哈希/字节数及所有 gate-bearing 字段，并逐字段比较，不接受
只修改旧 `gate_passed` 的摘要。当前 revalidation artifact 为
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-20260902-pre.8-v1.json`
（534 bytes，SHA-256
`8366977C0F3800D09C348DCA629DA43FD8DFC9815DD9009D03F0FB2E3CAC8C18`），
`source_recomputed=true`、`gate_passed=true`。输出契约由
`configs/schemas/simulator-external-evidence-revalidation.schema.json` 定义，回归位于
`tests/test_simulator_external_evidence.py`。

总验收脚本已把这个 revalidator 接成可重算绑定。使用
`run_goal_acceptance.ps1 -SimulatorExternalEvidence <join.json>` 时，输出的
`simulator_external_evidence` 节点会同时记录 join 与新生成的 revalidation 文件、两者的
字节数/SHA-256、期望与实际 release label、`source_recomputed`、verifier exit code 和
`gate_passed`；加入 `-RequireSimulatorExternalEvidence` 后，缺失路径、source 被替换、摘要
被编辑、版本不匹配或 verifier 失败都会让总验收 fail-closed。当前绑定快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260902-external-revalidation-v1.json`
（29,246 bytes，SHA-256
`73F85C769FDE560294575DC22140C67A85CD6D82717F6475088F40396C31F506`），脚本退出码 `0`，
核心测试收集 `2343`、QGC custom `66/66`，external binding 的
`provided/verified/source_recomputed/gate_passed` 均为 `true`。这是 software/SIM provenance
绑定；当前硬件链路仍为 `offline_pending`，所以不改变 HIL、REAL、室外 GNSS、人工视觉或最终
`v0.3.0` 发布门。
该快照绑定的 revalidation 输出是
`artifacts/evaluation/simulator-external-runtime-ai-revalidation-acceptance-20260902-pre.8-v1.json`
（534 bytes，SHA-256
`CA6A73B6012FC28669F696CB5C3D916BA8E7E10F8F59315D8ACDC9A90F9C442A`）。

## 最新台式机 Docker external runtime + live AI target（2026-09-01，pre.8）

当前台式机用固定 digest 镜像
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
完成了独立的 PX4/Gazebo 外部进程与 UDP I/O 重跑。runtime evidence 为
`artifacts/evaluation/simulator-docker-acceptance-20260901-desktop.json`（21,876 bytes，
SHA-256 `C4D191D46604BF86D1BD2BBA64F3907E2C35AB3F1F3369BEFFFCCA709305A045`），其
`gate_passed`、`preflight_gate_passed`、`runtime_gate_passed`、`telemetry.qualified` 和
容器 stop/rm 清理均为真，并观察到 PX4 HEARTBEAT 与 `GPS_RAW_INT`（fix 3、10 颗卫星）。

同一场景和镜像再执行 `--execute --exercise-arm --exercise-target`，live AI evidence 为
`artifacts/evaluation/simulator-docker-ai-acceptance-real-20260901-desktop.json`（106,789
bytes，SHA-256 `B53ECA7F8D8F0C4D830C656FA22D3C1C39BD6DAA1E0C8D5719CDE3A6C68733B7`）。
该文件要求并记录新鲜 armed HEARTBEAT 后 ARM 重试并成功、2 个 AI policy step、共享
`SimulatorMavlinkControlAdapter` 的 `MANUAL_CONTROL`、truth-free 目标观测的 `TRACK`
决策、DISARM 和清理；`ai_policy_gate_passed=true`、`target_policy_gate_passed=true`。

将本次台式机 VTOL pair 与已通过的 fixed-wing/multirotor pair 重新绑定到同一
`configs/release-parity.json` 后，三机型 join evidence 为
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260901-desktop-v2.json`
（5,370 bytes，SHA-256
`C2FFB02152A96026B639CCA7EE50C243B22BEC94A5FF41A0421A6A409DEA2382`），
`gate_passed=true`。校验器重新计算六份 source 的哈希，并检查统一镜像/版本、场景 manifest、
GPS 质量、truth-free perception、AI armed/`TRACK`、硬件控制关闭和容器清理。

随后按当前工作树执行的完整 goal acceptance 为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-desktop-v11.json`（14,892 bytes，
SHA-256 `2323A780DA7D62EC3AE267A4871B2D51FB7EBDF757294783FD300336D658158C`）；核心测试
收集 `2319`、QGC custom `66/66`，软件状态 `pass`，操作员凭据 `ready=true`，现场链路仍为
`field_link_ready=false` / `hardware_status=offline_pending`。该快照只刷新软件/静态门，不改变
人工 Fly View、室外 GNSS、真实设备、HIL/REAL 或最终发布门。

同一现场预检随后被绑定到新的总验收快照
`artifacts/evaluation/multidetect-goal-acceptance-20260901-desktop-v12.json`（23,195 bytes，
SHA-256 `D57149544BFA4CE614F42B8751E93CA5156DFE328D279EF48F793B994FC91F6A`）。快照重新收集
核心测试 `2319`、QGC custom `66/66`，并记录 source `provided=true`、`verified=true`；
其 `hardware_link_ready=false`、`gate_passed=false` 和 verifier 退出码 `1` 真实保留现场未
就绪状态，整体软件状态仍为 `pass`。这只完善证据绑定，不改变 REAL/HIL 发布门。

其后的一次有界只读现场预检写入
`artifacts/evaluation/field-link-preflight-20260901-desktop-rerun.json`（4,982 bytes，
SHA-256 `0A9C8561E2E91886A1555CE6F2BCE4AF2E1F720DCEC4AC969FFD5561F4A9586B`）。Jetson
`192.168.144.20:22`、GR01 `192.168.144.11:5760` 和相机 `192.168.144.108:554` 均超时，
本机 `14580/14581` 可绑定；`--require-all` 在首个失败样本后停止（请求 3、收集 1），
最终 `hardware_link_ready=false`。该报告只刷新现场网络事实，不改路由、不发送飞控或 RTSP
控制，也不能替代室外 GNSS、真实 Pixhawk/执行器、HIL/REAL 或人工 Fly View 验收。

上述两个 source 只关闭 software-only SIM external runtime 与 AI/target policy 子门。它们不
改变 `operator_visual_acceptance`、真实 Jetson/相机/室外 GNSS/Pixhawk/执行器、HIL、REAL 或
人工 Fly View 的状态；这些字段必须继续保持 `not_claimed`/`offline_pending`，也不能据此发布
最终 `v0.3.0`。

### 三机型共同 Mission transaction smoke（pre.8）

为了避免只验证 VTOL、却让 plain multirotor 或 fixed-wing 绕开事务门，仓库现在提供
`scripts/run_px4_airframe_transaction_acceptance.py`。它用同一个
`compile_executable_mission` dispatcher 生成三类 runtime-channel fixture，然后对每一类执行
完全相同的：

```text
AUTHORIZED → UPLOADING → MISSION_REQUEST_INT(每个 sequence) → ACK
→ READBACK_VERIFIED → COMMITTED → START_PERMITTED
```

每个分支都写独立的 `DurableMissionJournal`，并由
`scripts/verify_px4_airframe_transaction_acceptance.py` 重新打开 JSONL、比较字节数/SHA-256、
验证 hash-chain、终态、完整 sequence 和 identity binding。这个 smoke 是 software/SIM
contract gate，`physical_output_enabled=false`；它补齐三机型的共用路径证据，但不把内存
transport 升级成真实 Pixhawk、HIL、REAL 或室外 GNSS 验收。

```powershell
$env:PYTHONPATH = 'src;.'
python scripts\run_px4_airframe_transaction_acceptance.py `
  --out artifacts\evaluation\px4-airframe-mission-transaction-acceptance-latest.json
python scripts\verify_px4_airframe_transaction_acceptance.py `
  artifacts\evaluation\px4-airframe-mission-transaction-acceptance-latest.json
```

三种 `vehicle_class` 必须同时出现：`vtol`、`fixed-wing`、`multirotor`；旧的只含 compiler
或历史 combined SIM artifact 不会被自动补写成这份 journal 证据。

当前 pre.8 复核文件为
`artifacts/evaluation/px4-airframe-mission-transaction-acceptance-latest.json`（11,971 bytes，
 SHA-256 `C60BD72039D2E4DCC7056D47CDD6CB6857047C05C98DDF7204ECE869A8B6030E`）；三类 item 数量
分别为 VTOL 7、fixed-wing 5、multirotor 5，独立 verifier 的 `gate_passed=true`。

当前 pre.8 E7 清单已经把它绑定进去：
`artifacts/evaluation/release-gate-pre.8-current-20260901-airframe-transaction.json`（SHA-256
`92CA956279C4C5321B5C90FA8FBEE9B8956FA854C883E606F9B65E7D8FE2B4D7`），聚合报告为
`artifacts/evaluation/release-gate-pre.8-current-20260901-airframe-transaction-report.json`
（SHA-256 `CAACDE3AFF159101B0CACE91864F6C91B1F3944058F54195B309955F76DC5F60`）。
三机型子门通过；该清单整体仍因当前现场链路失败和 `prerelease` channel 保持
`release_allowed=false`。

本轮整体验收快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-rgb-slam-duplicate-contract.json`（15,084 bytes，
 SHA-256 `1E1A7156A24517513759F4640F44B8120A1891F273A3C3112E3E9D65A945C733`）。它收集 2,314
项核心测试、QGC 自定义 66/66，临时目录固定到 D 盘；软件状态为
`pass_with_environment_gap`、硬件状态为 `offline_pending`，不把 SIM/in-process 事务证据升级成
真实 Pixhawk、Jetson、室外 GNSS、HIL/REAL 或人工 Fly View 验收。

E7 清单可以把这份三机型 artifact 绑定到顶层
`px4_airframe_transaction`。传入
`--px4-airframe-transaction-evidence <airframes.json>` 后，writer 保存相对路径、字节数、
SHA-256、版本、三类机型和共享 dispatcher/transport；`multidetect-release-gate` 每次读取
清单都会重新哈希 source JSON，并调用同一 verifier 打开三份 durable journal。任何 journal
追加尾巴、摘要漂移、缺少 airframe 或 `physical_output_enabled` 被打开都会关闭该子门。
预发布清单可以省略该字段以兼容旧 evidence；切换到最终 `release` channel 时该绑定是必需的。

```powershell
python scripts\create_release_gate_manifest.py `
  --out artifacts\evaluation\release-gate-pre.8.json `
  --px4-airframe-transaction-evidence `
    artifacts\evaluation\px4-airframe-mission-transaction-acceptance-latest.json `
  ...四个模式 evidence 与 parity 参数...
```

这份绑定只关闭 software/SIM 的共同 Mission contract 子门，不把 in-process transport
升级为真实 Pixhawk、室外 GNSS、HIL/REAL 或人工 Fly View；最终发布仍需这些独立门全部通过。

REPLAY 门还可以在四模式清单顶层绑定一个严格的 `ReplaySetManifest`：传入
`--replay-set-manifest` 后，生成器会记录原始清单 SHA-256 和 canonical SHA-256；聚合器会重新
加载该清单，复核其内部 raw recording/真值哈希、帧对齐和六类部署域覆盖。清单存在但仍为
pending 时会直接让证据门保持关闭；旧的四模式预发布清单可以暂时省略该字段，但切换产品
版本到最终 `release` 通道时它是必需项。

REAL 门可以在顶层绑定现场链路预检报告 `real_field_link`。传入
`--real-field-link-preflight` 后，生成器会记录预检报告的相对路径、字节数和 SHA-256，以及
严格 verifier 的 `hardware_link_ready`/`gate_passed` 结果；可用
`--real-field-link-max-age-s` 覆盖默认 `900` 秒窗口。生成器同时记录 `max_age_s` 和源
报告的 `report_timestamp_epoch_s`，聚合器每次重新读取并哈希该文件，再调用同一
`verify_field_link_report(..., require_ready=True, max_age_s=...)`，并比较这两个 freshness
字段。因此报告替换、摘要漂移、缺失/未来/过期时间戳、watch 未完成或任一现场端点未就绪
都会关闭 evidence gate。预发布清单可以暂时省略该字段以保持旧清单兼容，但最终
`release` 通道必须提供带 `max_age_s` 与 `report_timestamp_epoch_s` 的通过记录，不能只靠
REAL mode 自报 `hardware_acceptance=passed`。

Windows 预发布 wrapper `scripts/run_current_release_gate_pre8.ps1` 对应提供
`-RealFieldLinkMaxAgeSeconds`，默认同为 `900` 秒，并按 invariant culture 透传给 manifest
生成器；因此命令行 wrapper 与 Python CLI 使用同一 freshness 语义。

v24 的实际透传复核使用 `-RealFieldLinkMaxAgeSeconds 1200`，输出目录为
`artifacts/evaluation/terminal-continuation-v24/e7-freshness-bound-1200/`。manifest
`release-gate-pre.8-current-20260904T062000Z-v6-bound.json` 的 SHA-256 为
`3A8F97E45F7A9F17E1ED88762AD4E029C9C3CF1425CF1CEA60FDBE330BDDFFC0`，report SHA-256 为
`74E9AC9FC1144127FC800C755D43C01EB1082E693954945535C774309C2D62B5`；`max_age_s=1200`
被写入并在 evaluator 重读时保持一致，`hardware_link_ready=false` 仍阻断现场门。

REAL 目标精度可以在顶层绑定离线的 `real_target_accuracy` 报告。传入
`--real-target-accuracy-report`（`--target-geolocation-accuracy-report` 为同义别名）后，
生成器会记录报告路径、字节数、SHA-256、样本数、WGS-84 P95 误差和 `1σ/2σ` 覆盖率。
聚合器每次读取清单都会重新哈希报告，并从报告内的独立测量样本重算北/东误差与距离
指标；报告的 `ground_truth_isolated=true`、`advisory_only=true`、
`contains_flight_control=false` 是强制契约。报告存在但失败或被替换时会关闭 evidence
gate；预发布清单可以省略该字段以保持旧清单兼容，但最终 `release` 通道必须提供通过的
`real_target_accuracy`。这份真值只用于现场精度验收，不会进入实时坐标、标定或飞控路径。
报告结构见 [`configs/schemas/target-geolocation-accuracy-report.schema.json`](../configs/schemas/target-geolocation-accuracy-report.schema.json)。
若估计坐标已经在已验证的 `target-event-replay` bundle 中，现场可以直接用
`scripts/build_target_geolocation_accuracy.py <manifest.json> <truth.csv> --output <report.json>`
生成同一格式；该入口会在评估前重验事件/媒体哈希，并拒绝 stale/unknown GPS、漏配事件和
跨目标 ID 配对。生成的报告还会携带 `selection` 诊断对象（源 manifest 哈希、选中/可用/
匹配/跳过计数及未匹配/未使用 ID），verifier 会校验计数守恒；旧报告省略该可选对象时仍
兼容读取。这样 E7 绑定的是重算后的报告，不是人工复制的一列坐标。

现场真值表可先由
`scripts/create_target_geolocation_survey_template.py <manifest.json> --output <truth.csv>`
生成；它只导出 fresh/qualified 事件并把真值列留空，输出的 JSON envelope schema 见
[`configs/schemas/target-geolocation-survey-template.schema.json`](../configs/schemas/target-geolocation-survey-template.schema.json)。

SIM S4 也可以在四模式清单顶层绑定 `simulator_safety`。传入
`--simulator-safety-evidence <simulator-safety-acceptance.json>`（或
`--sim-safety-evidence`）时，生成器会先执行独立 verifier，再写入相对路径、源文件
SHA-256、事件/版本身份、六个 check、verifier 事件和退出码。清单读取时聚合器会重新哈希
源文件并再次计算六个 check，同时比较清单声明与 verifier 输出；源文件、声明字段、check
映射或版本身份任一漂移都会让 evidence gate 关闭。这个绑定在 pre-release 中是可选的以
兼容旧清单，但切换到最终 `release` 通道时是必需项；它只关闭 SIM 软件安全子门，不代表
室外 GNSS、真实 Pixhawk/Jetson/相机、HIL/REAL 或人工 Fly View 已完成。

### 三机型 deterministic SIM 场景矩阵（pre.8）

`scripts/run_simulator_scenario_matrix.py` 会按稳定顺序发现
`configs/scenarios/*.scenario.json`，用同一时钟、AI、authority、watchdog 和 flight-model
契约分别运行 fixed-wing、multirotor、VTOL，并重复运行一次比较 evidence hash。它输出的
`simulator_scenario_matrix_finished` 文件仍是 truth-free software gate：
`external_3d_runtime`、QGC 视觉、硬件和实飞字段保持 `not_claimed`，不会把 Docker/SITL 或
静态运行升级成 REAL/HIL。

总验收调用该 runner 时会附加 `--cover-faults --fault-tail 1.0`。runner 读取每个 runtime
channel 的声明故障，把共同 frame budget 自动延长到最后一个事件，并在每个场景写入
`fault_coverage`（声明数、实际触发数、`active_event_ids`、`fired_event_ids`、未覆盖 ID 和
覆盖率）；`--require-fault-coverage` 在预算不足或事件未被观测时直接关闭矩阵门。有限时长
故障的 `fired` 由第一次观测变换置真，避免只有无限时长故障才有触发标记。故障覆盖仍属于
SIM 软件证据，不替代外部 3-D、真实 GNSS、硬件或人工 Fly View。

```powershell
$matrix = ".\artifacts\evaluation\simulator-scenario-matrix-acceptance-latest.json"
& $py scripts\run_simulator_scenario_matrix.py --out $matrix
& $py scripts\verify_simulator_scenario_matrix.py $matrix `
  --expected-release-label 0.3.0-pre.8
```

验证器会重算每个场景摘要、三机型覆盖、AI accepted step、repeatability、故障覆盖、truth-free 状态和
文件内 canonical `sha256`。`create_release_gate_manifest.py` 传入
`--simulator-scenario-matrix-evidence <matrix.json>` 后，将相对路径、文件字节数/SHA-256、
canonical SHA-256、版本、场景 ID/模式、AI/arm/repeatability 开关、claims 和 verifier checks
写入顶层 `simulator_scenario_matrix`；`multidetect-release-gate` 每次读取都会再次哈希并复核
源文件。预发布清单可省略此字段以兼容旧证据，但最终 `release` channel 要求三机型矩阵存在且
通过，不能用单场景、`--no-ai`、`--no-arm` 或 `--no-repeat` 诊断替代。最终通道还强制
`fault_coverage_required=true` 与 `fault_coverage_auto_extended=true`，并重新验证每个声明
故障的 `fired`/`active_frames`；因此即使哈希自洽，nominal-only 或手工缩短预算的矩阵也会
保持关闭。

当前覆盖跑的源证据为
`artifacts/evaluation/simulator-scenario-matrix-20260901-pre.8-fault-coverage.json`（13,512
bytes，SHA-256 `8DD6171CB60528B800BEEF98021E7C83AC00580792016A14EF27AFB94D531A7E`），自动预算
2,611 帧，fixed-wing/VTOL/multirotor 的声明故障分别为 4/2/3，均已触发且覆盖率为 `1.0`。
它已由 `run_goal_acceptance.ps1` 绑定进最新总验收
`artifacts/evaluation/multidetect-goal-acceptance-20260901-fault-coverage-v5.json`（36,169
bytes，SHA-256 `7DC28AA4A36204C82C58680E7637C873859B93F1298762F6BC3EB2A072B72C41`）；总脚本退出码
`0`，核心测试收集 `2337`，QGC custom `66/66`。该快照还强制绑定并重新验证联合 SIM source
`artifacts/evaluation/combined-sim-qgc-20260901-continue-recovery-v9/combined-evidence.json`
（333,051 bytes，SHA-256 `75265742413146EA78AB88CCBD30B22ACFCC0C5DC7EC48780F4A9D25F649F7FB`，
`gate_passed=true`）和 release-mode envelope
`artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v3.json`
（730 bytes，SHA-256 `ACDFB988D7BE669FFEEC4846BDA6F66837B7892CF402DA1D25B01DB84D92A045`）。该快照同时绑定已验证的只读现场预检
`artifacts/evaluation/field-link-preflight-20260901-continue-v2.json`（4,987 bytes，SHA-256
`13BB57F062F2C9DA687EECBAC3616A1C73291B837FE134DAFBEA20505EEE6328`），但 Jetson/GR01/相机
端点不可达，预检 verifier 退出码 `1`，`hardware_link_ready=false` / `gate_passed=false`。
这仍是 prerelease 的 truth-free SIM 软件证据，不会关闭真实 GNSS/Jetson/Pixhawk、HIL/REAL 或人工
Fly View 门。

当前 pre.8 E7 矩阵换绑诊断清单为
`artifacts/evaluation/release-gate-pre.8-current-20260901-fault-coverage-matrix-bound.json`
（7,796 bytes，SHA-256 `E5377B0CCA3EC1BE24A3FA436FD1FCEAFC440FF13BC7630C61C114578200E92D`），
现行 verifier 报告为
`artifacts/evaluation/release-gate-pre.8-current-20260901-fault-coverage-matrix-bound-v2-report.json`
（15,470 bytes，SHA-256 `C7D92E59C8B24C41B04F8CD4DC9246C34ADA674A7D7E91361551336C96993EAD`）。
报告中三机型矩阵与 9/9 故障覆盖子门为 `passed=true`，总 `release_allowed=false` 是预期结果：
当前现场、REAL/replay-set/目标精度和 prerelease channel 仍未满足最终发布条件。

```powershell
& $py scripts\create_release_gate_manifest.py `
  --out .\artifacts\evaluation\release-gate-pre.8-matrix-bound.json `
  --parity-json .\configs\release-parity.json `
  --replay-evidence .\artifacts\evaluation\replay-acceptance.json `
  --sim-evidence .\artifacts\evaluation\sim-acceptance.json `
  --hil-evidence .\artifacts\evaluation\hil-acceptance.json `
  --real-evidence .\artifacts\evaluation\real-acceptance.json `
  --simulator-scenario-matrix-evidence $matrix `
  --allow-incomplete
```

本轮最终通道故障覆盖策略与 verifier 回归后，全量 Python 回归为
`2336 passed, 1 skipped`（退出码 `0`）；原始日志封存于
`artifacts/evaluation/pytest-full-20260901-final-fault-coverage-policy.log`（2,901 bytes，
SHA-256 `F9CDC0D3CC26F276B13585B11923199780E4EFE949559D28CDB1A03B54C0D9F8`）。唯一跳过项
是 Windows 账户无法创建 symlink 的 release-parity 测试；它不会被记作通过。

## 统一 GPS/GNSS 质量契约（pre.8）

GPS 质量门的 wire-domain 常量和归一化入口位于
`src/multidetect/mavlink_gps.py`，共享评估函数位于
`src/multidetect/adaptive_ranging.py::assess_gps_quality`。SIM/live bridge、Pixhawk、回放
和现场会话验证器必须使用同一规则：`GPS_FIX_TYPE` 为 `3..8`、卫星数为已知 `6..254`（`255`
是 unknown）、`hAcc≤6 m`、`vAcc≤10 m`、坐标为 `latitude∈(-90,90)` / `longitude∈[-180,180]`，
且观测时间有限、非未来、年龄不超过 `1.5 s`。`position_healthy` 不是 `true` 时即使字段齐全
也不合格。

`GPS_RAW_INT` 的 fix、卫星数、坐标、`alt`、`eph` 和时间戳按原子样本更新；缺失/非法字段、
降级 fix、未知卫星哨兵、极点坐标或未来时间会清除 raw freshness，禁止旧坐标与新质量字段
拼接。`GLOBAL_POSITION_INT` 可以继续作为独立融合位置源，但永远不会替 raw GPS 质量门背书。
该契约只决定“可定位/不可定位”及证据是否落图，不改变 REAL/HIL 的发布门：没有真实 GNSS、
Pixhawk、Jetson、相机和人工 Fly View 证据时，最终 `release_allowed` 仍保持 `false`。

现场接线后的原始 GNSS 样本由 `multidetect-gnss-capture` /
`scripts/capture_gnss_samples.py` 只读记录；它仅调用 `recv_match()`，保存主机接收时间和
`GPS_RAW_INT`/`GPS2_RAW`/`GLOBAL_POSITION_INT` 字段，`messages_transmitted` 固定为 `0`。
`multidetect-gnss-capture-verify` / `scripts/verify_gnss_capture.py` 离线重算记录计数、合格
样本数、解码错误和质量门，结果可作为后续目标事件回放/RTK 真值调查的输入，但不会直接填充
`real_field_link`、关闭 E7 或把单独 GNSS 窗口写成 REAL 证据。

当前权威的 pre.8 总验收快照是
`artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-contract-v4.json`（SHA-256
`1EB9D735CEB9FD9400F780074278952EFF81E20990AD83769E2AF6BD11620002`）。它收集 `2271` 个核心
测试（`2268 passed / 3 skipped`），QGC 自定义回归为 `66/66`；固定翼与 S4 子证据分别绑定
`fixed-wing-software-acceptance-20260901-gps-contract-v4.json`（SHA-256
`1EE64A535A1C64FD41A4EA02F3164F5A62514B0ADF9006DF5CBE0C5D9215FF4C`）和
`simulator-safety-acceptance-20260901-gps-contract-v4.json`（SHA-256
`021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`）。同一构建的 QGC
toolbar drawer 还提供只读 `GPS/GNSS`、`RANGE_STATUS` 质量行，显示门原因与 source/data age，
不创建第二套 UI 或发送飞控指令。该快照绑定最新现场预检
`artifacts/evaluation/field-link-preflight-20260901-v3.json`（SHA-256
`C5C4F0405FE9D61E373550FBEE9DB929580838FAEF417BCCEB99E11D19AF0D05`），现场链路仍为
`hardware_link_ready=false`，所以快照的 `software_status=pass_with_environment_gap`、
`hardware_status=offline_pending`，不关闭室外 GNSS、真实硬件、HIL/REAL 或最终发布门。

## 最新外部 Combined SIM/QGC/Mission 证据（pre.8）

当前台式机在同一份 `v0.3.0-pre.8` portable 构建上完成了 900 帧 Docker PX4/Gazebo/QGC
联合验收。source evidence 为
`artifacts/evaluation/combined-sim-qgc-20260901T074256Z-48728/combined-evidence.json`，
大小 `772,736` bytes，SHA-256
`4A50E88770FF1B2084A92D72E228C4C1913E49E69BAAFE46BA43B742939D774F`，`gate_passed=true`。
该 run 覆盖动态九端口 loopback、900 帧传感器、VTOL `MC→FW→MC`、7 项 INT-only Mission
upload/readback/active、Mission barrier、原生 QGC toolbar/video machine smoke 以及
Docker stop/rm 和端口释放。独立 verifier 输出
`artifacts/evaluation/combined-sim-qgc-20260901T074256Z-48728/combined-evidence-verification.json`
（SHA-256 `B2855FD4C7B64DA178676A00B01EFB3E05969681ED01B9DE597D130883D1FCD7`）的 33/33
check 全部通过。

对应的 SIM envelope 为
`artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports-v2.json`
（SHA-256 `F608493C7F1385D070B8CCAE903444F4028B78FF21AB30152C9C05958BC8ABC0`）。最新 E7
预发布清单/报告为
`artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v3.json`
（SHA-256 `8B78B49D7F857051FB171C5A64C2DC6D920990B926004291A7472C9745F0B1D7`）/
`artifacts/evaluation/release-gate-pre.8-current-20260901-combined-qgc-v3-report.json`
（SHA-256 `8DECBFB949C2B250311A9489CB781EC7BBA8376FF1D1C2B137F934BA14E67F99`）。
清单中的 parity 五项、SIM combined 子门和 S4 子门通过，但整体
`gate_passed=false`/`release_allowed=false` 仍是预期结果：REAL 现场链路、REPLAY 最终
精度、HIL/REAL、人工视觉和最终 `release` channel 继续未完成。

同一次 source/envelope 绑定进完整 goal acceptance 的最新快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-combined-qgc-v3.json`
（SHA-256 `64E92ADB8B99033520EE27703BAC442505828E1EA12B3E56D211864C66EC7882`），核心
测试 `2271`（`2268 passed / 3 skipped`），QGC custom `66/66`，软件/硬件状态仍为
`pass_with_environment_gap`/`offline_pending`。这些证据只关闭 software-only SIM 子门，
不能替代真实 Pixhawk、Jetson、相机、室外 GNSS、执行器、HIL/REAL、人工 Fly View 或实飞。

## SIM S4 安全子门（pre.8）

四模式清单之外，当前 `v0.3.0-pre.8` 的 goal acceptance 还必须执行一次独立的 S4
安全证据并通过严格复核。它把生产的目标选择、控制 authority、watchdog、围栏/GPS 门和
Mission 终态放进同一条 deterministic SIM 路径，专门锁住以下高风险回归：

* `CANCEL_TRK` 只清除被取消的目标；全量取消后 active/tracked/locked/primary/evidence
  全部为空，重新选择才允许再次捕获；
* QGC/PX4 仅接受 literal loopback 和 allowlist 消息，Mission/flight write 为零；watchdog
  进入 `FAILSAFE` 后只发一帧中立输出，AI 不能再注入控制；
* allowed-zone、geofence、position unhealthy 和 WGS-84 极点/越界坐标全部 fail-closed；
  `TERMINATED` 之后不接受任何 Mission event。

入口、验证器和 JSON Schema 分别为：

```text
scripts/run_simulator_safety_acceptance.py
scripts/verify_simulator_safety_evidence.py
configs/schemas/simulator-safety-evidence.schema.json
```

直接运行：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
python scripts\run_simulator_safety_acceptance.py `
  --out artifacts\evaluation\simulator-safety-acceptance-latest.json
python scripts\verify_simulator_safety_evidence.py `
  artifacts\evaluation\simulator-safety-acceptance-latest.json
```

`run_goal_acceptance.ps1` 会自动运行这两个入口并把路径、SHA-256、复核退出码和六个 S4
check 写入总快照。该证据只表示软件/SIM fail-closed 门；其中
`hardware_acceptance`、`operator_visual_acceptance`、Pixhawk/Jetson/相机和真实飞行字段
必须保持 `not_claimed`，不能把 S4 结果提升为外部 PX4/Gazebo、室外 GNSS、HIL、REAL 或人工
Fly View 通过。

本轮完整 goal acceptance 已绑定该 S4 子门：
`artifacts/evaluation/multidetect-goal-acceptance-20260901-s4-safety-v11.json`，SHA-256
`38627B36E6522DFFF76ED7B7BFECC0E2C7F795938C1C45FD48CFD25CBFA0BCDE`，核心测试 `2209`、
QGC 自定义 `66/66`，脚本退出码 `0`。其中
`simulator_safety_acceptance.gate_passed=true`、`verifier_exit_code=0`，S4 artifact SHA-256
为 `021D627162F99A836AD1C7C7724AF1DB56FF7901F7298381457EA0B91BB32DEC`。台式机现场报告仍
是 `hardware_link_ready=false`、`verification_exit_code=1`，因此总快照保持
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，最终
`v0.3.0` 发布门继续关闭。

## Combined SIM 动态端口契约（pre.8）

Windows PX4/Gazebo/QGC 组合 runner 的 JSON 体量很大，单看其顶层
`gate_passed=true` 不足以证明端口生命周期和证据边界没有漂移。独立验证器
`src/multidetect/simulator_combined_evidence.py`（仓库入口
`scripts/verify_combined_sim_evidence.py`，安装后命令
`multidetect-sim-combined-evidence`）固定核对：九个 `30000..60000` 动态 loopback 端口的
数量/唯一性/命名顺序、生产端口 `14550` 的前后快照与排除、`all_test_ports_released`、
Docker stop/rm、传感器/VTOL/Mission 嵌套门，以及所有硬件输出和 operator visual claim
必须保持关闭。源文件修改、布尔门篡改、端口复用或声明人工/真实硬件都会使验证失败。

要求 E7 绑定时，同时传入 `--release-mode-evidence` 和
`--require-release-mode`；验证器会解析 `sim_release_mode_evidence_prepared`，将 envelope 的
`source.path` 与 `source.sha256` 重新绑定到同一个 combined JSON：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
python scripts\verify_combined_sim_evidence.py `
  artifacts\evaluation\combined-sim-qgc-20260901T051239Z-76220\combined-evidence.json `
  --expected-release-label 0.3.0-pre.8 `
  --release-mode-evidence artifacts\evaluation\release-mode-sim-20260901-pre.8-combined-dynamic-ports.json `
  --require-release-mode `
  --out artifacts\evaluation\combined-sim-qgc-20260901T051239Z-76220\combined-evidence-verification.json
```

当前复核输出为
`artifacts/evaluation/combined-sim-qgc-20260901T051239Z-76220/combined-evidence-verification.json`
（SHA-256 `E8440B7B4DDD5981A1A8B7FD279CF210AE016E1858D008B9DE8D5DD507BFFCA2`，33/33 check
通过）。`create_release_gate_manifest.py` 新增
`--simulator-combined-evidence` 与
`--simulator-combined-release-mode-evidence`，生成顶层 `simulator_combined` 记录；
`check_release_gate.py` 在每次读取清单时重新哈希两个文件并复核。当前接线清单/报告为
`artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-v2.json`
（SHA-256 `C026AE1A3209BFE4798DDD600A12EBC4F1684DCE49E871DF5F28D80253D5A188`）和
`artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-v2-report.json`
（SHA-256 `47222082B48FFB8E20AB1264A78CC4C25F6FFBBD9F0321AF17C18E420A65F0FD`）。
该清单的 `simulator_combined.passed=true`，但总门仍因现场链路和 prerelease channel
保持 `gate_passed=false`、`release_allowed=false`；这个 SIM 证据不提升为 HIL/REAL、室外
GNSS 或人工 Fly View 验收。

总验收脚本也支持把同一 Combined SIM source/envelope 绑定进 goal-acceptance 快照：

```powershell
Set-Location D:\Projects\Multi-Detect
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_goal_acceptance.ps1 `
  -CombinedSimEvidence .\artifacts\evaluation\combined-sim-qgc-20260901T051239Z-76220\combined-evidence.json `
  -CombinedSimReleaseModeEvidence .\artifacts\evaluation\release-mode-sim-20260901-pre.8-combined-dynamic-ports.json `
  -RequireCombinedSimEvidence `
  -AllowMissingOperatorCredentials
```

输出 JSON 的 `simulator_combined_acceptance` 会保存 source/envelope 路径、大小、SHA-256、
verifier 退出码、完整验证结果和原因。省略三个 Combined SIM 参数时保持旧版软件验收
兼容；传入 `-RequireCombinedSimEvidence` 时，路径缺失、文件替换、envelope 错绑或任一
复核失败都会使脚本 fail-closed。该绑定仍然只表示 software-only SIM provenance，不改变
真实现场、室外 GNSS、HIL/REAL 或人工 Fly View 的状态。

为了在不覆盖 canonical `*-latest` provenance 的情况下重跑总验收，脚本还接受
`-FixedWingAcceptanceOutput <path>` 和 `-SimulatorSafetyOutput <path>`。传入后两类子门会写入
调用方指定的版本化文件；不传时继续使用原有 `fixed-wing-software-acceptance-latest.json`
和 `simulator-safety-acceptance-latest.json` 路径。
当前版本化重跑快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-versioned-combined-binding.json`
（SHA-256 `F8E82527E0F048BE8EC60F244EC75E1B96C4FFE5AD49C5DDCAD50EBB81F12559`）。该快照把
Combined-SIM source/envelope 与现场预检都绑定到同一份总验收，Combined-SIM 子门通过，现场
`hardware_link_ready=false`，所以整体仍保持 `software_status=pass_with_environment_gap` /
`hardware_status=offline_pending`。

## 模式证据封装

各 runner 输出的 JSON 通常包含视频、传感器、ACK、审计和清理等详细字段，而 E7 清单只接受
一个可重算的模式身份和显式 `gate_passed`。中间适配器
`scripts/create_release_mode_evidence.py`（实现位于 `src/multidetect/release_mode_evidence.py`）
把一个**明确指定**的 runner 文件封装成 deterministic、hash-bound envelope：

```text
runner JSON → create_release_mode_evidence.py → release-mode-<mode>.json
            → create_release_gate_manifest.py → check_release_gate.py
```

封装器会保存源文件相对路径、SHA-256、事件、源运行模式、源 `gate_passed` 和原因；源 gate
缺失、不是布尔值或为 `false` 时，输出 gate 一律为 `false`，不会从文件名、goal snapshot、
fixture 或状态字段推断通过。源文件没有 `runtime_mode`，或声明的模式与 `--mode` 不同，必须
分别显式传入 `--allow-missing-source-runtime-mode` 或
`--allow-source-runtime-mode-mismatch`；这两个开关只记录 caller projection，不改变源 gate。
Envelope 的机器可读契约是
`configs/schemas/release-mode-evidence.schema.json`。
安装 editable package 后，同一入口也注册为 `multidetect-release-mode-evidence`；在 clean
checkout 或未安装 package 的环境中继续使用仓库 wrapper，二者调用同一 `main()`。

例如，把已有的四类 runner 证据装配为当前 pre.8 的中间文件：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'

& $py scripts\create_release_mode_evidence.py `
  --mode replay `
  --source .\artifacts\evaluation\integrated-replay-20260901-pre.8.json `
  --out .\artifacts\evaluation\release-mode-replay-20260901-pre.8.json
& $py scripts\create_release_mode_evidence.py `
  --mode sim `
  --source .\artifacts\evaluation\combined-sim-qgc-20260831T234111Z-53232\combined-evidence.json `
  --out .\artifacts\evaluation\release-mode-sim-20260901-pre.8-rerun.json
& $py scripts\create_release_mode_evidence.py `
  --mode hil `
  --source .\artifacts\evaluation\fixed-wing-software-acceptance-latest.json `
  --out .\artifacts\evaluation\release-mode-hil-20260901-pre.8.json `
  --notes 'software-only HIL preflight; hardware and real-control remain unclaimed'
& $py scripts\create_release_mode_evidence.py `
  --mode real `
  --source .\artifacts\evaluation\field-link-preflight-20260901-continue-20260901-053931.json `
  --out .\artifacts\evaluation\release-mode-real-20260901-pre.8.json `
  --allow-source-runtime-mode-mismatch --allow-incomplete `
  --notes 'offline field-link preflight; keep REAL gate closed'
```

随后由四模式清单生成器和聚合器重新计算 envelope SHA-256。旧的无 `-field-rerun` 清单仍保留
为历史 provenance；当前权威 pre.8 装配结果是：

| 文件 | SHA-256 | 当前含义 |
| --- | --- | --- |
| `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun.json` | `A13D0223504221C986645F7EE73C6F3B15F8B1656687CE66421FCA8BD80F0A94` | 四模式清单；SIM envelope 绑定 2026-09-01 最新 900 帧成功 run，现场报告为同日最新只读预检，状态仍有 `not_claimed` |
| `artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-report.json` | `746FBF1CD8D144D532E67B7AAF0E0E615A03B11397801DC190A88E5767F4AD43` | `gate_passed=false`、`release_allowed=false`；parity 五项一致，SIM/HIL runner source gate 为真，现场链路未就绪 |
| `artifacts/evaluation/release-gate-pre.8-current-20260901-s4-bound.json` | `A00EAA2EC6AA8CCEF3A05692998932C77FE060667E42AFA72DD21B8326B448FF` | pre.8 接线快照；顶层 `simulator_safety` 绑定 S4 artifact，六个 check 与 verifier 均通过；REAL 现场链路仍未就绪 |
| `artifacts/evaluation/release-gate-pre.8-current-20260901-s4-bound-report.json` | `38357A92648DC07D1597B76AE912F6BCC61930D4B8E13AF18A666260EFDAF224` | 聚合器重新哈希/复核 S4 后 `simulator_safety.passed=true`；整体仍 `gate_passed=false`，不关闭现场门 |

### 动态 loopback 端口重跑（2026-09-01，最新 SIM provenance）

组合入口 `scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1` 已从固定 host 端口切换为
`30000..60000` 有界动态分配：九个端口逐个执行 TCP/UDP loopback bind 探测，排除容器内 PX4
输入 `18570` 和受保护的地面站 UDP `14550`，并在 evidence 中记录分配列表与释放状态。
Docker inspect stop/rm 同时设置 `20 s` 超时，避免 Docker Desktop API 故障把清理阶段拖成无界等待。

本轮权威 SIM 源证据、envelope 和 E7 绑定结果如下：

| 文件 | SHA-256 | 结果 |
| --- | --- | --- |
| `artifacts/evaluation/combined-sim-qgc-20260901T051239Z-76220/combined-evidence.json` | `4DE2F6AD236EBE65F97CD9A279A101453FA9C938D45E25B9A0FB2DF0D3B7F4F1` | 772,886 bytes；900 帧；`gate_passed=true`；VTOL attach/Mission/QGC barrier/cleanup 通过 |
| `artifacts/evaluation/release-mode-sim-20260901-pre.8-combined-dynamic-ports.json` | `86B6A7B4E220D7E05C8C9E56A678B22B269587AB758D05DE8BBEE855644FD981` | SIM envelope；源 gate 为真，动态端口和 software-only 边界保留 |
| `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound.json` | `08C3B431C57329855051BDF3D5FF6FD8F8DBE54F8D09E474EB36563ADEF2B13E` | pre.8 E7 清单；SIM envelope 与 S4 哈希绑定 |
| `artifacts/evaluation/release-gate-pre.8-current-20260901-combined-dynamic-ports-s4-bound-report.json` | `5A5D7E0435F8906E692ADB3A44D6D7A972A5CCF381AC960B0E92DB3D7B123298` | `simulator_safety.passed=true`；整体 `gate_passed=false`、`release_allowed=false` |

该 run 分配 host 端口
`38392, 32511, 46147, 30192, 55621, 30275, 49253, 53375, 31727`，并记录
`all_test_ports_released=true`、`protected_udp_14550_unchanged=true`。它只更新 SIM software
 provenance；`hardware_link_ready=false`、REAL/室外 GNSS/真实 Pixhawk/Jetson/相机、人工 Fly View
和最终 `v0.3.0` release channel 仍保持 fail-closed。此前的 field-rerun 清单继续保留为现场
链路 provenance，不能用本轮 Docker/SITL 结果替换现场门。

随后只读重跑现场链路预检，生成
`artifacts/evaluation/field-link-preflight-20260901-post-sim.json`（SHA-256
`2B3EF846CE463BDEBABAC4F1246AA08D3D1587E356C0AEB470A0196667FACC63`，退出码 `0`）。
Jetson/GR01/RTSP 三个默认 TCP 探针仍超时，`14580/14581` 本机 UDP bind 成功，
`all_passed=false`、`hardware_link_ready=false`。该诊断文件未绑定为通过的 REAL source，
因此不会改变本清单的 fail-closed 结果。
绑定该诊断后的总 goal-acceptance 快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-post-dynamic-ports.json`（SHA-256
`BB795D37E03D55FFEC7B4C9EA742BE1F8CC06E81EBCFF4A3475A6E3C52F7738F`），核心测试 `2216`、
QGC 自定义回归 `66/66`、脚本退出码 `0`；软件状态为 `pass_with_environment_gap`，硬件为
`offline_pending`。它只刷新源码/现场 provenance，不改变本清单 `gate_passed=false` 的结果。

在当前台式机上完成有界网段发现后，又生成了以当前 parity 为准的绑定清单：

| 文件 | SHA-256 | 当前含义 |
| --- | --- | --- |
| `artifacts/evaluation/release-gate-pre.8-current-20260901-subnet-discovery.json` | `93C15C0BE48EF4EAEB1ED259A62321BC4F69998B5B13CD61801594812ABC77EB` | 四模式 pre.8 清单；五组 parity 一致，`real_field_link` 绑定 `field-link-discovery-20260901-continue-subnet.json`，现场未就绪 |
| `artifacts/evaluation/release-gate-pre.8-current-20260901-subnet-discovery-report.json` | `0416E67F56EE143FC903446E8D18DCEDC2925D0EECE810F939321D1BC5A41BF5` | `gate_passed=false`、`release_allowed=false`；REPLAY 精度/人工视觉/HIL/REAL/最终 release channel 继续 fail-closed |

随后又对当前机器上两个显式 IPv4 CIDR 做了有界、只读的服务发现。`10.8.8.0/24` 扫描了 `254`
个地址，响应主机和 SSH/MAVLink/RTSP 命中均为 `0`；原始报告为
`artifacts/evaluation/field-link-discovery-20260901-10-8-8.json`（SHA-256
`9D3294DD03D365E2D8AC6081EDBB91F0A92A4AB010F59F133BB91440A9D56281`）。
`192.168.1.0/24` 同样扫描 `254` 个地址，发现 `192.168.1.1`、`192.168.1.62`、
`192.168.1.77` 的 TCP/22 响应，但 `5760` MAVLink 和 `554` RTSP 均为 `0` 命中；原始报告为
`artifacts/evaluation/field-link-discovery-20260901-192-168-1.json`（SHA-256
`0A510A0D9036C9E1146345CEC4D928AC9D543F088A5A2E7CD6B90BE70E3DDE8F`）。这些候选只用于
诊断，不能据此选择 Jetson、GR01 或相机，也不会修改配置端点或提升 `hardware_link_ready`；接通
飞机 Ethernet 后仍需对实际地址执行普通的显式 field-link preflight。

该清单只刷新源码 parity 与现场链路 provenance；三种 Docker external runtime/AI target 证据仍作为
SIM 子门的独立 artifact 保存，不把 truth-free 合成目标、SSH 候选或 Docker/SITL 结果升级为
真实 Jetson、室外 GNSS、Pixhawk、执行器、人工 Fly View 或最终 `v0.3.0`。

本轮另外生成了一个只用于接线验证的 SIM replay staging 清单：
`artifacts/replay-sets/sim-gazebo-2026/replay-set.manifest.json`（SHA-256
`C2078C778BE12E688CD903339C1CA46E698AE5BCAF83675FD01BDC9AFDC537D3`）。它由已通过门禁的
Gazebo 相机捕获复制成六个分层，每层 5 帧，使用 `source_kind=synthetic_demo` 和显式空的
真值/预测；结构和哈希均有效，但没有独立标注，故 `deployment_domain_evidence_complete=false`
且不构成精度结论。带入四模式的中间清单
`release-gate-pre.8-current-20260901-replay-staging.json`（SHA-256
`1860EF768C6E9C645CFE7023078A177C7985A207BF9C27760A52871441AC4A1A`）及报告
`release-gate-pre.8-current-20260901-replay-staging-report.json`（SHA-256
`E1C1E0FB3D8A9D0EA8997ABBAD4124C59EFE795B8FBE91FC391884B95189C2E2`）后，聚合器会明确显示
`replay_set.present=true`、`passed=false`。它不是当前 field-rerun 权威清单，也不关闭最终发布门。

为补齐 replay-set 的绑定可见性，随后生成了同一 `pre.8` 版本的 replay-bound 补充清单：
`artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-replay-bound.json`
（SHA-256 `261CB2755E55013278E9BDEA698AC80D110B55A09F79193C52DD91A4AB27B75C`）及报告
`artifacts/evaluation/release-gate-pre.8-current-20260901-field-rerun-replay-bound-report.json`
（SHA-256 `DDE5D24A785D07AE1F40D21EBA8D27B379EBD0B5CAE5CBC9633786D50FDA96C8`）。该补充聚合结果
明确为 `replay_set.present=true`、`replay_set.passed=false`：六层数据仍是 `synthetic_demo`，
没有人工标注与独立精度复核；现场 `hardware_link_ready=false` 也保持不变。它只修正证据绑定的
可见性，不替换 field-rerun 权威清单或关闭任何发布门。

回放精度评估器对空基线也保持 fail-closed：即使六层目录、预测文件和复核标记都存在，
只要聚合 precision 或 recall 为 `null`（没有可计算的真值正样本与阈值内预测正样本），
`metrics_gate_passed`、`accuracy_validated` 和 `gate_passed` 仍为 `false`，并记录
`aggregate metrics precision/recall are undefined`。因此当前 `synthetic_demo` 空流只证明
接线与负例回归，不会被误读为检测精度通过。

E7 S4 绑定接线快照为
`artifacts/evaluation/release-gate-pre.8-current-20260901-s4-bound.json`（SHA-256
`A00EAA2EC6AA8CCEF3A05692998932C77FE060667E42AFA72DD21B8326B448FF`），报告为
`artifacts/evaluation/release-gate-pre.8-current-20260901-s4-bound-report.json`（SHA-256
`38357A92648DC07D1597B76AE912F6BCC61930D4B8E13AF18A666260EFDAF224`）。聚合器重新哈希并
复核后 `simulator_safety.present=true`、`passed=true`、六个 check 全为真；REAL 现场链路
仍未就绪，故整体 `gate_passed=false`。绑定实现后的总验收重跑为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-s5-release-gate-safety-v12.json`
（SHA-256 `C1FED75A38FAD315AE167FA47457F44D7D882E64A50CA1AFE63DBAE79A795DEA`），核心测试
`2215`、QGC 自定义 `66/66`、脚本退出码 `0`。
随后针对坏路径/哈希异常的 fail-closed 健壮性修复再次重跑：
`artifacts/evaluation/multidetect-goal-acceptance-20260901-s5-release-gate-safety-v13.json`
（SHA-256 `20C790F384918B8457CC3A2BA54401B20FF55B7786214D82A2CF385864308134`），核心测试
仍为 `2215`、QGC 自定义 `66/66`、脚本退出码 `0`；S4 绑定通过，现场链路仍为
`hardware_link_ready=false`/`offline_pending`。

绑定最新 `192.168.1.0/24` 只读发现报告后再次执行的总验收为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-network-discovery-v14.json`
（SHA-256 `A6CEAEDFA6AE583B118E8E69BF39102E13FB903A0E1BE0542379970C4C952D25`），核心测试
`2215`、QGC 自定义 `66/66`、S4 verifier 退出码 `0`，脚本退出码 `0`。该快照的
`field_link_preflight.report_sha256` 为 `0a510a0d9036c9e1146345cec4d928ac9d543f088a5a2e7cd6b90be70e3dde8f`，
但 `hardware_link_ready=false`，所以只刷新现场 provenance，整体发布门仍关闭。

同一现场报告与 S4 artifact 也已装配为新的 pre.8 清单：
`artifacts/evaluation/release-gate-pre.8-current-20260901-network-discovery-s4-bound.json`
（SHA-256 `D4E9412EED3F6CF2EA8E6DB11D1F81AC4A35826B7F343C682C4EFEAF619E631D`），聚合报告为
`artifacts/evaluation/release-gate-pre.8-current-20260901-network-discovery-s4-bound-report.json`
（SHA-256 `98BD437EEBEB0383901403B377E3B04C69D4DDDF2EAADDDC2AA97DCDAA52E66A`）。该清单重新哈希
并复核 S4 六项 check，`simulator_safety.passed=true`；由于现场链路未就绪，整体
`gate_passed=false`/`release_allowed=false`。

在这之后重新执行的完整 goal-acceptance 快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-network-refresh.json`（SHA-256
`7D070D0F49547D0569308959E8184E944FEDDAB7831C4A13B9BF659D2CC18458`）。该快照收集
`2167` 项核心测试（`2166 passed / 1 skipped`）、QGC 自定义 `66/66`，并把当前物理网卡与
现场报告绑定到同一 provenance：`Ethernet=Up`、`192.168.1.13/24`，没有
`192.168.144.0/24` 路由，`Ethernet 2` 断开。Jetson、GR01、相机和 `hardware_link_ready`
仍未就绪，因此当前 `field-rerun` 清单仍是发布门权威状态，`gate_passed=false`、
`release_allowed=false`；版本继续为 `v0.3.0-pre.8`。

HIL envelope 当前 SHA-256 为
`4F905FBC6221A5A7442DAA8F9507B427F7ED3782585A62625E04AA8C030BCDDA`，其源文件为本轮
fixed-wing software acceptance；每次 goal-acceptance 刷新该源文件后，都必须重新封装 HIL 并重建
四模式清单，避免源 SHA-256 漂移。

`verify_field_link_report(...)` 在加载现场证据时会按原始 `probes` 重算单次报告的摘要一致性；watch
报告则对每个 sample 执行同一检查，并要求聚合字段与完整样本集合一致。校验器使用 `utf-8-sig`
读取 JSON（兼容 Windows BOM），但 SHA-256 始终来自原始文件字节，所以编码兼容不会改变
`real_field_link` 的证据绑定。摘要被手工改成通过而 probe 失败时，校验结果保持
`gate_passed=false`。

本轮校验器/PowerShell 入口修复的最新完整 goal-acceptance 快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-field-verifier-hardening-v2.json`，
SHA-256 `A0FE03A238DF5CBC84E513D560C6FFE64CFE790679D0D85DE02C00B9AD069217`；该快照收集
`2166` 项核心测试、QGC 自定义 `66` 项并通过 QML/C++/portable smoke，但
`hardware_status=offline_pending`，不改变四模式 `gate_passed=false`。

本轮 SIM 源证据为
`artifacts/evaluation/combined-sim-qgc-20260831T234111Z-53232/combined-evidence.json`，SHA-256
`5194CC77F45572483B6A17A21FC8FC0DBE222BE62B9A8EB1CC14777C1B464F6C`；对应 envelope
`artifacts/evaluation/release-mode-sim-20260901-pre.8-rerun.json` 的 SHA-256 为
`9862E26984487822DF9DCCB1D0673889C28ACD50DE1DFB02385AC829E11D516F`。该 run 使用 900 个有界
传感器帧，Docker/PX4/Gazebo、相机/CameraInfo/NavSat readiness、VTOL 转换、PX4 Mission
transaction、QGC 原生 `VideoOutput`/PiP/fixed-wing QQuickItem 抓帧和清理均通过；
`operator_visual_acceptance` 仍为 `not_claimed`，所以机器截图不被提升为人工 Fly View 验收。

最新单次现场链路预检证据为
`artifacts/evaluation/field-link-preflight-20260901-continue-latest.json`（2351 bytes，SHA-256
`67FAD84F26C1DDC0301CD0197284AC70FE93281BC7773A9597093B58EFF68AC2`）。Jetson
`192.168.144.20:22`、GR01 `192.168.144.11:5760` 与相机 `192.168.144.108:554` 均超时，
本机 `14580/14581` 可绑定，`hardware_link_ready=false`；该报告已绑定到当前
`release-gate-pre.8-current-20260901-field-rerun.json`（SHA-256
`A13D0223504221C986645F7EE73C6F3B15F8B1656687CE66421FCA8BD80F0A94`）及其聚合报告
`release-gate-pre.8-current-20260901-field-rerun-report.json`（SHA-256
`746FBF1CD8D144D532E67B7AAF0E0E615A03B11397801DC190A88E5767F4AD43`）。

这两个文件不会把 runner 的 transport/software gate 提升为人工 Fly View、室外 GNSS、真实
Pixhawk、执行器或 HIL/REAL 证据；恢复现场链路、完成视觉确认和真实 Mission/控制验收后，必须
用新的源文件重新封装并重建清单，不能手工改 hash 或状态。

## 生成四模式清单

用 `scripts/create_release_gate_manifest.py` 只从四个**明确指定**的模式证据生成清单。
脚本不会扫描 `artifacts/`，也不会从 goal snapshot、fixture 或 dry-run 文档推导状态；证据
必须是 JSON 对象，声明对应的 `runtime_mode`，并且有明确的布尔 `gate_passed` 字段；字段为
`false` 时仍会原样保持关闭。证据文件的
SHA-256 在写入前重新计算，清单中的路径自动改成相对于清单输出目录的路径。

预发布清单可以用 `--allow-incomplete` 保留 `not_claimed`/`pending` 状态；
`configs/schemas/release-gate-manifest.schema.json` 对这些状态使用统一枚举，实际是否
通过仍由 `check_release_gate.py` 根据四模式、现场绑定和最终版本 channel 重新计算。这样
预发布诊断既能通过结构 schema 校验，又不会把未完成的人工、硬件或 Mission 验收升级为
发布通过。

五个共享 parity hash 可以放在一个 JSON 文件中（直接对象或 `{ "parity": { ... } }`），
也可以在命令行逐项提供。仓库提供独立的确定性生成器
`scripts/create_release_parity.py`，它只读取显式的源码/契约文件集合，不递归扫描仓库：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'

# 生成默认的 configs/release-parity.json（可重复运行）
& $py scripts\create_release_parity.py `
  --root . `
  --out .\configs\release-parity.json
```

默认清单（五组均为非空、按 POSIX 相对路径排序）如下；它是代码中的显式常量，便于审计和
代码审查：

```text
ui_contract
  ground-control/MultiDetectGCS/custom/CMakeLists.txt
  ground-control/MultiDetectGCS/custom/custom.qrc
  ground-control/MultiDetectGCS/custom/res/Custom/MultiDetect/MultiDetectState.qml
  ground-control/MultiDetectGCS/custom/res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml
  ground-control/MultiDetectGCS/custom/res/Custom/MultiDetect/MultiDetectVideoOverlay.qml
  ground-control/MultiDetectGCS/custom/src/CustomPlugin.cc
  ground-control/MultiDetectGCS/custom/src/CustomPlugin.h
  ground-control/MultiDetectGCS/custom/src/FlyViewCustomLayer.qml
  ground-control/MultiDetectGCS/custom/src/FlyViewVideo.qml
  ground-control/MultiDetectGCS/custom/src/MultiDetectConfigure.qml
  ground-control/MultiDetectGCS/custom/src/MultiDetectOperatorController.cc
  ground-control/MultiDetectGCS/custom/src/MultiDetectOperatorController.h
  ground-control/MultiDetectGCS/custom/src/MultiDetectOperatorProtocol.cc
  ground-control/MultiDetectGCS/custom/src/MultiDetectOperatorProtocol.h
  ground-control/MultiDetectGCS/custom/src/MultiDetectTargetEvidenceStore.cc
  ground-control/MultiDetectGCS/custom/src/MultiDetectTargetEvidenceStore.h
  ground-control/MultiDetectGCS/custom/src/PlanViewRightPanel.qml
  ground-control/MultiDetectGCS/custom/src/SelectViewDropdown.qml
core_algorithm
  src/multidetect/adaptive_ranging.py
  src/multidetect/depth_calibration.py
  src/multidetect/metric_depth.py
  src/multidetect/navigation_fusion.py
  src/multidetect/simulator/perception_bridge.py
  src/multidetect/target_event_replay.py
  src/multidetect/target_geolocation.py
task_contract
  configs/schemas/autonomy-platform-neutral-plan.schema.json
  src/multidetect/domain.py
  src/multidetect/mission.py
  src/multidetect/operator_protocol.py
  src/multidetect/operator_tracking.py
safety_policy
  configs/schemas/autonomy-operator-grant.schema.json
  src/multidetect/authorization.py
  src/multidetect/safety.py
  src/multidetect/simulator/control.py
  src/multidetect/simulator/control_gateway.py
  src/multidetect/simulator/mavlink_control.py
  src/multidetect/state_machine.py
px4_mission_path
  configs/schemas/autonomy-executable-mission.schema.json
  src/multidetect/operator_mavlink.py
  src/multidetect/payload_hil_mission.py
  src/multidetect/payload_target_gate.py
  src/multidetect/px4_mission.py
```

`v0.3.0-pre.8` 的历史 parity 快照（在 perception bridge 共享入口收口前）保留在
`artifacts/evaluation/release-parity-pre.8-20260901-outdoor-piecewise-v32.json`；该历史输入
文件 SHA-256 为 `4ED355018B24A093FB7276E26DFACEBCCB5C826BF2DD04A6B0491EB810A91B0F`。
当前源码清单已按最新 `SimulatorPerceptionBridge` 委托路径重新生成到
`configs/release-parity.json`，当前文件 SHA-256 为
`B37A138C9CBE31E70D2A756F2B2B7A63DE81DC218F5401F2018204E01CD5E33A`，五组 hash 为：

```text
ui_contract_hash       8f10a8f469b2f61952ca1db617b3b0ab14f149a12af1f8d3e68757d60f71b221
core_algorithm_hash    6c2d186e8b6c31330b16fae096c4d90302797bdf5d697e28ff4efbc55fbdc06d
task_contract_hash     965c7bb34e606af824a7f71318d4d8fa9c929655d616ff0b829e919e621ab7f1
safety_policy_hash     f781b90021938e5b9d45056eb48c6367599401ee65f2f8f0279f0aaa03bab2d3
px4_mission_path_hash  86e91ead9b9480961df043a392627d9d2880680b6744c265c69764eedd35b067
```

最新测距时间接缝回归绑定在
`artifacts/evaluation/multidetect-goal-acceptance-20260901-target-ai-v4.json`（SHA-256
`D4B0EE2E7F274B0904DE2CDB60AC42DA4195CFAFE31D1774418A8180E46DE5CF`）：完整脚本退出码 `0`，
核心测试收集 `2183`、QGC 自定义测试 `66/66`，并验证 `SimulatorPerceptionBridge` 对显式
range data/source age 的 `1.5 s` fail-closed 门。该快照的 `hardware_link_ready=false`、
`hardware_status=offline_pending` 和人工 Fly View=`not_claimed` 保持不变，因此不能关闭
REAL/HIL/室外 GNSS 或最终发布门；Python-only 改动也不改变已构建的 portable EXE。

随后 SIM AI 返航门接入独立 raw-GPS 新鲜度：`gps_observed_at_s` 必须存在、非未来且年龄不超过
`maximum_return_gps_age_s=1.5 s`，缺失/过期/未来样本均保持 `return_gps_quality_insufficient`
fail-closed。fresh-boundary、missing、stale、future 回归和共享 acceptance 均通过；最新总验收
绑定为 `artifacts/evaluation/multidetect-goal-acceptance-20260901-field-recheck-v5.json`
（SHA-256 `7EAE02CC318160650673508692C8D5F5C6636ADBADA1ACED2BBF098661FAE70`）。本轮把
`src/multidetect/simulator/ai_executor.py` 纳入 `core_algorithm` parity；现场硬件仍
`offline_pending`，因此 `REAL/HIL/室外 GNSS` 和最终 `v0.3.0` 发布门继续阻断。

返航 GPS 新鲜度门落盘后，完整 pre.8 总验收再次执行并绑定到
`artifacts/evaluation/multidetect-goal-acceptance-20260901-gps-return-freshness-v6.json`
（SHA-256 `B43DC058C34A7ACEC7E489DEF0BFC55FD8D9A7348FB30D2F5B3F54B256F4189C`），核心测试
收集 `2188`、QGC 自定义测试 `66/66`，退出码 `0`。现场报告结构验证通过但
`hardware_link_ready=false`，所以只关闭软件回归门，未关闭任何真实硬件、室外 GNSS、HIL/REAL
或最终版本门。

随后按当前源码分别重跑三种机型的确定性 3-D asset/control gate：VTOL
`river_rescue_vtol_001` 的证据为
`artifacts/evaluation/simulator-3d-control-acceptance-20260901-river_rescue_vtol_001-continue.json`
（SHA-256 `1D4F90E0C631BCB42A7F9DF5BD6F9C4B7468CD37AAA891BF96AF8CF11DD64177`），fixed-wing
`coastal_patrol_fixed_wing_001` 为
`artifacts/evaluation/simulator-3d-control-acceptance-20260901-coastal_patrol_fixed_wing_001-continue.json`
（SHA-256 `FB879AE4F627474B24D3397739A6E3ECC60F92891CCF671CC846414D02697982`），multirotor
`urban_inspection_multirotor_001` 为
`artifacts/evaluation/simulator-3d-control-acceptance-20260901-urban_inspection_multirotor_001-continue.json`
（SHA-256 `84419C67A853D6467A900CC76154EE1699052123B456C0C1D0BC66B006ABFCFE`）。三份报告均为
`gate_passed=true`、`deterministic_gate_passed=true`；本机 `px4`/`gz` 仍不可用，故
`external_runtime_gate_passed=false`，不提升外部 Gazebo、视觉、真实硬件、室外 GNSS、HIL/REAL
或人工 Fly View 门。

Docker Desktop daemon 恢复后又按固定镜像
`px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8`
执行了三种机型的 disposable external runtime gate。对应 runtime-preflight 为
`artifacts/evaluation/simulator-runtime-preflight-20260901-continue.json`（文件 SHA-256
`0B304D5CCEE184AC25747BD85B08D2310D4A764DF6D253E0C150B9F48746D5D6`），记录
`execution_mode=docker`、`docker_daemon_ready=true`、`ready_to_attempt=true`；native
`px4`/`gz` 仍不在 PATH：

| 机型 | evidence | SHA-256 | 关键状态 |
|---|---|---|---|
| VTOL | `artifacts/evaluation/simulator-docker-acceptance-20260901-continue-vtol.json` | `391BDA5D7832F08174B8260D9B1B43ADC6BB5FB3E67E82AC5EE6F1859905E16D` | `gate/runtime/telemetry/cleanup=true` |
| fixed-wing | `artifacts/evaluation/simulator-docker-acceptance-20260901-continue-fixed-wing.json` | `FC20E7A241E53A7BBE82B4498D86BD06D2AA614D4AD44A1AD57464A3A11F4A22` | `gate/runtime/telemetry/cleanup=true` |
| multirotor | `artifacts/evaluation/simulator-docker-acceptance-20260901-continue-multirotor.json` | `D3CA94B17C3A1461E059D078020BFA1010930257A76A18CE28FA8935040BD75F` | `gate/runtime/telemetry/cleanup=true` |

同一镜像的 live AI target acceptance 使用 `--execute --exercise-arm --exercise-target`，三种
机型分别绑定：

| 机型 | evidence | SHA-256 | 关键状态 |
|---|---|---|---|
| VTOL | `artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-vtol.json` | `C60E759BC99E5BF9D5190CE711B9B93F219880A7A7ED2534D797E394A3CC1B57` | `ai/target/arm/disarm/cleanup=true`, `ai_steps=2`, `action=track` |
| fixed-wing | `artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-fixed-wing.json` | `99FBF23B71A94A21A9F6EB34A43D7737FEC651DAF5E3E267E837C7A0F0CB4032` | 同上 |
| multirotor | `artifacts/evaluation/simulator-docker-ai-acceptance-20260901-continue-multirotor.json` | `0633659951E05C9BD5384251AE95D7D834FEE076CE782D6197CF79167A6D032A` | 同上 |

六份 evidence 关闭的是 SIM external runtime 与 AI/target policy 子门；目标输入仍是
truth-free 合成 perception，故 `operator_visual_acceptance`、相机/Jetson、室外 GNSS、真实
Pixhawk/执行器、HIL/REAL 和实飞状态不从这些文件推断。当前清单仍是 prerelease
`v0.3.0-pre.8`，最终 `v0.3.0` 继续要求真实现场与人工验收。

为了让六份 evidence 的一致性有单一可复核入口，运行
`scripts/verify_simulator_external_evidence.py` 会把三种机型的 runtime/AI 文件和当前 parity
绑定为一个独立的 SIM join artifact。校验器重新计算每个输入的 SHA-256，并要求同一 digest-
pinned 镜像、同一场景/manifest、pre.8 版本、`GPS_RAW_INT fix_type>=3`/卫星数、truth-free
perception、硬件控制关闭、AI target/armed/track 和容器 stop/rm 清理。当前输出为
`artifacts/evaluation/simulator-external-runtime-ai-evidence-20260901-pre.8.json`（SHA-256
`43D34D1E7697CDB950816B9C1057104EB4F90820C60AF1A5163B081174BC3632`），`gate_passed=true`。
该文件只证明 SIM external runtime 与 AI/target policy 子门的文件级一致性，不改变四模式
release-gate 清单，也不把 truth-free、Docker/SITL 或静态证据升级为人工 Fly View、室外 GNSS、
真实 Pixhawk/Jetson/相机、执行器、HIL/REAL 或最终 `v0.3.0`。

该 join 后的完整 pre.8 软件门快照为
`artifacts/evaluation/multidetect-goal-acceptance-20260901-s3-faults-v10.json`（SHA-256
`A3E2CC98F3B3117D3C36E1FEF15DAA2E9CAB41E4ADB31352FA0397CA74BFEDE5`），核心测试 `2200`、QGC
自定义回归 `66/66`、脚本退出码 `0`；portable simple-boot 日志
`artifacts/evaluation/qgc-simple-boot-20260901-external-join-v8.log`（SHA-256
`B329814985CF63C04460E2F8DD9872CFCF2A96D48DB96DF051564578287F0F91`）也退出 `0`。快照仍为
`software_status=pass_with_environment_gap`、`hardware_status=offline_pending`，不会改变
REPLAY 标注/精度、人工 Fly View、室外 GNSS、HIL/REAL 或最终 `v0.3.0` 发布门。

当前 canonical 文件由 `scripts/create_release_parity.py` 可重复生成；此前的
`release-parity-pre.8-20260901-target-geo-pole.json`、
`release-parity-pre.8-20260831-vtol-alias-dispatcher.json`、
`release-parity-pre.8-20260831-depth-grid-fix.json` 以及
`release-parity-pre.8-20260830-gps-sentinel-constants.json`、
`release-parity-pre.8-20260830-gps-quality-gateway-qgc-age-world-origin.json`、
`release-parity-pre.8-20260830-gps-quality-gateway-drain.json`、`release-parity-pre.8-20260830-gps-quality.json`、
`release-parity-pre.8-20260830-gps-replay.json` 和 `release-parity-pre.8-20260830.json` 只作为此前源码状态的历史快照保留。

这些 hash 只绑定当前显式源码集合；契约文件变化后必须重新运行生成器并审阅差异，不能把
旧 hash 复制到新的四模式清单。

每个组的 SHA-256 输入按固定 framing 计算：`MD-PARITY-V1\0`、组名 UTF-8 长度和内容、
文件数量，然后对每个排序后的相对路径写入路径 UTF-8 长度、路径和经过 CRLF/CR→LF 规范化后的
内容长度及内容（长度均为 8 字节 big-endian 无符号整数）。除换行符外不改动内容。路径必须位于
`--root` 内；绝对路径、`..`、符号链接逃逸以及 `artifact(s)`、`fixture(s)`、`build` 等生成目录
都会被拒绝。需要替换清单时，使用 `--file-list-json` 传入显式 JSON（可直接给五个组的对象，或
`{ "schema_version": 1, "source_files": { ... } }`），不要用 glob 或把 evidence/fixture/dry-run
结果纳入 parity。

以下命令生成当前产品的预发布清单；`--allow-incomplete` 允许状态仍为 `not_claimed` 时写出
清单，严格发布检查仍由 `check_release_gate.py` 完成：

下面示例中的 `replay-acceptance.json`、`sim-acceptance.json`、`hil-acceptance.json` 和
`real-acceptance.json` 是由调用方先行生成的占位路径，仓库不会自动创建或从 goal snapshot、
fixture、Docker/SITL 记录推断它们。替换为实际 evidence 前直接运行会按设计 fail-closed（退出码
`2`）；`--allow-incomplete` 只影响状态不完整的现有证据，不会绕过缺失文件。

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'

& $py scripts\create_release_gate_manifest.py `
  --out .\artifacts\evaluation\release-gate-pre.8.json `
  --parity-json .\configs\release-parity.json `
  --replay-evidence .\artifacts\evaluation\replay-acceptance.json `
  --sim-evidence .\artifacts\evaluation\sim-acceptance.json `
  --hil-evidence .\artifacts\evaluation\hil-acceptance.json `
  --real-evidence .\artifacts\evaluation\real-acceptance.json `
  --replay-visual passed --replay-mission passed `
  --sim-visual passed --sim-mission passed `
  --hil-visual passed --hil-mission passed `
  --real-visual passed --real-mission passed `
  --hil-hardware passed --hil-real-control passed `
  --real-hardware passed --real-real-control passed `
  --simulator-safety-evidence .\artifacts\evaluation\simulator-safety-acceptance-latest.json `
  --replay-set-manifest .\artifacts\replay-sets\fire-smoke-2026\replay-set.manifest.json `
  --allow-incomplete
```

现场链路预检通过后，把同一份保存的 JSON 绑定进清单。该绑定不能从 `REAL` mode 的状态字段
推导，必须显式传入，并且生成器会在写入前用严格 verifier 重查 `hardware_link_ready`：

```powershell
& $py scripts\create_release_gate_manifest.py `
  --out .\artifacts\evaluation\release-gate-pre.8-real-link.json `
  --parity-json .\configs\release-parity.json `
  --replay-evidence .\artifacts\evaluation\replay-acceptance.json `
  --sim-evidence .\artifacts\evaluation\sim-acceptance.json `
  --hil-evidence .\artifacts\evaluation\hil-acceptance.json `
  --real-evidence .\artifacts\evaluation\real-acceptance.json `
  --replay-visual passed --replay-mission passed `
  --sim-visual passed --sim-mission passed `
  --hil-visual passed --hil-mission passed `
  --real-visual passed --real-mission passed `
  --hil-hardware passed --hil-real-control passed `
  --real-hardware passed --real-real-control passed `
  --real-field-link-preflight .\artifacts\evaluation\field-link-preflight-20260901.json `
  --real-target-accuracy-report .\artifacts\evaluation\target-geolocation-accuracy-20260901.json `
  --real-target-state-evidence-report .\artifacts\evaluation\field-session-real.capture.json `
  --simulator-safety-evidence .\artifacts\evaluation\simulator-safety-acceptance-latest.json `
  --replay-set-manifest .\artifacts\replay-sets\fire-smoke-2026\replay-set.manifest.json `
  --allow-incomplete
```

`--allow-incomplete` 只允许在证据尚未齐全时生成预发布清单；它不会把未就绪的现场链路
标记成通过。聚合器每次读取清单都会重新计算 `real_field_link.report_sha256` 和
`report_bytes`，并校验报告内容与记录的 readiness/gate 一致。报告被替换、摘要漂移、watch
样本不完整或未就绪时，清单会立即失败；最终 `release` 通道还会把缺少该绑定作为发布阻断原因。

若要同时绑定 deterministic SIM S4 安全证据，在生成命令中加入：

```powershell
--simulator-safety-evidence .\artifacts\evaluation\simulator-safety-acceptance-latest.json
```

生成器会在写入前运行 `verify_simulator_safety_evidence.py` 同等的严格校验；失败的源文件
或 verifier 不会生成可声称通过的 `simulator_safety` 记录。聚合器随后仍会重新哈希并复核，
最终 `release` 通道缺少该绑定时会明确阻断。

如果不使用 parity JSON，必须一次性提供以下五项（不能只给其中几项）：

```powershell
--ui-contract-hash <64-hex> `
--core-algorithm-hash <64-hex> `
--task-contract-hash <64-hex> `
--safety-policy-hash <64-hex> `
--px4-mission-path-hash <64-hex>
```

状态参数接受 `passed`、`not_required`、`not_claimed` 和 `pending`。省略状态时，视觉与
Mission 默认为 `not_claimed`；HIL/REAL 的硬件与 real-control 默认为 `not_claimed`，
REPLAY/SIM 的两项默认为 `not_required`。因此省略或填写未完成状态会把对应模式的
`gate_passed` 保持为 `false`。默认严格退出码为 `2`（清单仍会原子写入）；仅在明确使用
`--allow-incomplete`/`--report-only` 时，生成不完整清单返回 `0`。

脚本也接受 `--replay`、`--sim`、`--hil`、`--real` 作为 evidence 参数的短别名，以及
`--status MODE.FIELD=STATUS` 的重复式状态写法（例如 `--status real.visual=passed`），以及
`--no-overwrite` 防止替换现有清单。生成后仍要运行聚合器检查四模式、parity、状态和产品
版本通道：

```powershell
& $py scripts\check_release_gate.py .\artifacts\evaluation\release-gate-pre.8.json `
  --report-only --out .\artifacts\evaluation\release-gate-report.json
```

两个 wrapper 也支持从仓库根目录直接调用；它们在直接执行时会自动把仓库的 `src/`
加入导入路径，因此不依赖 `PYTHONPATH` 或已安装的 console script（Python 环境本身仍需
满足项目依赖）：

```powershell
python scripts\create_release_gate_manifest.py --help
python scripts\check_release_gate.py --help
```

聚合器默认把当前 UTC 写入 `evaluated_at_utc`，方便记录检查发生的时间。为了让同一份
清单在复核、归档或 CI 重放时生成完全相同的 JSON 字节，可以显式传入带时区的
`--evaluated-at-utc`；`Z` 和带偏移的 ISO-8601 写法都会规范化为 UTC：

```powershell
& $py scripts\check_release_gate.py .\artifacts\evaluation\release-gate-pre.8.json `
  --report-only `
  --evaluated-at-utc 2026-09-02T00:00:00Z `
  --out .\artifacts\evaluation\release-gate-report-reproducible.json
```

固定时间只影响报告元数据，不会跳过任何证据哈希、现场链路、GPS/RANGE 新鲜度或最终
发布门；缺少时区的时间戳会直接失败。这样可用同一个清单、同一个证据集合和同一个时间
戳重建报告并逐字节比对 SHA-256。

## 清单格式

清单只引用证据，不复制证据内容。相对路径以清单所在目录为基准；每个证据文件都会重新
计算 SHA-256，并检查 JSON 内的布尔 `gate_passed` 与 `runtime_mode`；只有
`gate_passed=true` 的证据记录才会让聚合门通过，`false` 会保留为失败原因。五个 parity
hash 必须在四个模式中完全一致。

`runtime_mode`/`mode` 与 `event` 在读取时会先去除首尾空格并按小写比较；空白值仍按缺失或
不合法处理。这样生成器与严格评估器对同一份证据保持一致的身份语义。

```json
{
  "schema_version": 1,
  "product": "Multi-Detect",
  "release_label": "0.3.0-pre.8",
  "modes": {
    "replay": {
      "evidence_path": "replay.json",
      "evidence_sha256": "<64 hex characters>",
      "gate_passed": true,
      "parity": {
        "ui_contract_hash": "<64 hex characters>",
        "core_algorithm_hash": "<64 hex characters>",
        "task_contract_hash": "<64 hex characters>",
        "safety_policy_hash": "<64 hex characters>",
        "px4_mission_path_hash": "<64 hex characters>"
      },
      "operator_visual_acceptance": "passed",
      "hardware_acceptance": "not_required",
      "real_control_acceptance": "not_required",
      "mission_path_acceptance": "passed"
    }
  }
}
```

`modes` 必须同时包含 `replay`、`sim`、`hil`、`real`。上面的 JSON 只展示一个记录，不能
直接作为完整发布清单。`HIL` 和 `REAL` 的 `hardware_acceptance`、`real_control_acceptance`
必须为 `passed`；`REPLAY`/`SIM` 只能把硬件字段写成 `not_required` 或独立证据中的
`passed`。所有模式的人工 Fly View 与 Mission 路径状态必须为 `passed`，不能使用
`not_claimed`/`pending` 冒充最终发布证据。

现场链路绑定记录只保存报告的身份和严格验证结果，不复制预检明细：

```json
{
  "real_field_link": {
    "report_path": "../evaluation/field-link-preflight-20260901.json",
    "report_sha256": "<64 hex characters>",
    "report_bytes": 12061,
    "max_age_s": 900,
    "report_timestamp_epoch_s": 1788471147.3144484,
    "hardware_link_ready": true,
    "gate_passed": true
  }
}
```

`report_path` 以 release-gate 清单所在目录为基准；`report_sha256`、`report_bytes`、
`max_age_s` 和 `report_timestamp_epoch_s` 必须匹配磁盘文件及 verifier 结果。聚合器会调用
与现场采集相同的 `verify_field_link_report`，所以仅修改这些字段无法伪造就绪状态。预发布
清单可以省略 `real_field_link` 或旧 freshness 字段以保持读取兼容；切换到最终 `release`
通道时，记录必须存在、freshness-bound 且所有检查通过。

目标坐标精度绑定记录只保存离线报告的身份和摘要；完整样本仍保留在报告文件中：

```json
{
  "real_target_accuracy": {
    "report_path": "../evaluation/target-geolocation-accuracy-20260901.json",
    "report_sha256": "<64 hex characters>",
    "report_bytes": 24876,
    "evidence_type": "multidetect.target-geolocation-accuracy.v1",
    "schema_version": 1,
    "ground_truth_isolated": true,
    "advisory_only": true,
    "contains_flight_control": false,
    "gate_passed": true,
    "sample_count": 10,
    "p95_error_m": 4.2,
    "one_sigma_coverage": 0.8,
    "two_sigma_coverage": 1.0
  }
}
```

`report_path` 以 release-gate 清单目录为基准；`report_sha256` 和 `report_bytes` 必须匹配
磁盘文件。聚合器会重新运行 `verify_geolocation_accuracy_report()`，所以只修改绑定摘要
或报告中的 `gate_passed` 都不能伪造通过。报告要求至少 10 条唯一样本；产品 P95/覆盖率
阈值由生成报告时的 `requirements` 显式记录，未指定阈值的报告只能表达样本完整性，不能
替代现场精度目标。预发布清单可以省略该绑定；最终 `release` 通道要求记录存在且通过。

带有 `replay_set` 时，其结构如下；`manifest_path` 相对于 release-gate 清单所在目录，两个
哈希分别绑定磁盘上的 JSON 字节和 `ReplaySetManifest.canonical_hash`：

```json
{
  "replay_set": {
    "manifest_path": "../replay-sets/fire-smoke-2026/replay-set.manifest.json",
    "manifest_sha256": "<64 hex characters>",
    "canonical_sha256": "<64 hex characters>",
    "gate_passed": true,
    "accuracy_validated": false,
      "accuracy_evidence": {
        "path": "../replay-sets/fire-smoke-2026/accuracy-report.json",
        "sha256": "<64 hex characters>",
        "gate_passed": true,
        "accuracy_validated": true
      },
      "calibration_verification": {
        "path": "../replay-sets/fire-smoke-2026/threshold-calibration-verification.json",
        "sha256": "<64 hex characters>",
        "event": "replay_set_threshold_calibration_verified",
        "schema_version": 1,
        "report_path": "../replay-sets/fire-smoke-2026/threshold-calibration.json",
        "report_sha256": "<64 hex characters>",
        "manifest_sha256": "<64 hex characters>",
        "canonical_sha256": "<64 hex characters>",
        "report_promotion_ready": false,
        "recomputed_promotion_ready": false,
        "gate_passed": true
      }
  }
}
```

`accuracy_validated=false` 是刻意保留的独立质量门：回放集结构和部署域覆盖通过后，仍需
逐帧 IoU evaluator 的独立报告来关闭模型精度/阈值验收；该字段不能被 release-gate 清单手工
改成 `true`。报告由 `scripts/evaluate_replay_set.py` 生成，包含回放集 raw/canonical
摘要、逐层 metrics、显式阈值和独立复核状态；release-gate 会重新检查报告文件哈希、事件、
摘要绑定以及 `gate_passed=true`/`accuracy_validated=true`。最终 `release` 通道必须同时
提供这个嵌套证据，预发布可以先只绑定结构清单。阈值校准的独立完整性复核可以通过
`--replay-set-calibration-verification <verification.json>` 追加绑定；writer 和 evaluator
都会重跑报告请求并核对同一组 raw/canonical SHA-256。这个子门允许
`report_promotion_ready=false`，因为它只证明校准报告没有漂移，不能替代部署域 IoU 精度
与人工复核门。

### 当前 fail-fast 现场绑定（2026-09-02，pre.8）

当前台式机的现场预检报告为
[`field-link-preflight-20260902-stop-on-failure-v3.json`](../artifacts/evaluation/field-link-preflight-20260902-stop-on-failure-v3.json)
（6,684 bytes，SHA-256
`D06796570C08FC8DB5342788D6578EEC95B1DA5FAD598FC0DA1B4A51D0AAEE5E`）。它请求 3 个
watch 样本，在第 1 个失败样本后停止：`samples_collected=1`、`stop_reason=failure`、
`route_checks_requested=true`、`route_checks_passed=null`、`hardware_link_ready=false`。
`null` 是未观察完剩余窗口的明确状态，不是路由通过值；完整 watch 或 one-shot 才会产生
布尔路由聚合。

该报告的身份绑定清单为
[`release-gate-pre.8-current-20260902-field-link-watch-v3.json`](../artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-watch-v3.json)
（10,336 bytes，SHA-256
`E498B45064682280DE3F82A9BA479CD5B4BBD261BABB56D16B45CCB00638AFBA`），固定时间
report-only 输出为
[`release-gate-pre.8-current-20260902-field-link-watch-v3-report.json`](../artifacts/evaluation/release-gate-pre.8-current-20260902-field-link-watch-v3-report.json)
（20,260 bytes，SHA-256
`5CF73EA62905AD41F3FABEC0C904A5C558681AB978589051C9512BAF15EE9254`）。复核报告无 route
类型误报，只保留现场 `hardware_link_ready=false` 阻断；因此 `gate_passed=false`、
`release_allowed=false`，不会把桌面诊断升级为 REAL/GNSS 设备验收。

## 六层现场证据 bundle（pre.8）

现场证据现在可以通过 `scripts/check_field_evidence_bundle.py` 做一次独立、只读的
hash-bound join。它要求并重新验证以下六层：

| 层 | 绑定内容 | 关键 fail-closed 条件 |
|---|---|---|
| `field_link` | Jetson/GR01/RTSP/metadata 预检报告 | `hardware_link_ready` 与 verifier gate 必须为 `true` |
| `gnss` | receive-only `GPS_RAW_INT/GPS2_RAW` 报告 | 至少一个合格 raw GPS；摘要计数必须和记录一致 |
| `execution` | 同一会话的 `DET → TRK → LCK → TGT`、取消、重捕获 | 严格 TGT 状态证据、取消后重捕获、嵌入链路 hash 全部匹配 |
| `target_event` | GPS、`RANGE_STATUS`、视频/深度的目标事件 manifest | GPS fresh/qualified、距离有效且媒体 hash 可重算 |
| `media_timeline` | raw recording、telemetry sidecar、目标 frame 绑定 | sidecar 与 recording 逐样本相等，帧只使用 at-or-before telemetry，年龄不超过 `1.0 s` |
| `accuracy` | 独立 WGS-84 真值误差报告 | manifest hash/字节、事件 ID、真值行和 sample count 完整守恒 |

推荐命令（所有路径必须来自同一现场窗口）：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src'
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'
# 先将七个变量设置为同一现场窗口的实际报告路径
# $fieldSessionReport / $gnssCaptureReport / $targetEventManifest /
# $recordingManifest / $telemetrySidecar / $linkPreflightReport / $accuracyReport
& $py scripts\check_field_evidence_bundle.py `
  --field-session-report $fieldSessionReport `
  --gnss-capture-report $gnssCaptureReport `
  --target-event-manifest $targetEventManifest `
  --recording-manifest $recordingManifest `
  --telemetry-sidecar $telemetrySidecar `
  --link-preflight $linkPreflightReport `
  --accuracy-report $accuracyReport `
  --out artifacts\evaluation\field-evidence-bundle-$(Get-Date -Format yyyyMMdd-HHmmss).json
```

严格检查通过时退出码为 `0`，待办/缺件时为 `2`；`--report-only` 只把待办结果写盘并
返回 `0`。`--allow-sim-domain` 仅用于 scenario-bound recording 的 SIM 时间轴诊断。
报告固定 `release_eligible=false`、`hardware_acceptance=not_claimed`、
`real_flight_acceptance=not_claimed` 和 `contains_flight_control=false`，所以完整
bundle 也不能被 E7 聚合器解释成 HIL/REAL、室外 GNSS 或最终版本发布证据。JSON 契约位于
`configs/schemas/field-evidence-bundle.schema.json`；Python API 为
`multidetect.field_evidence_bundle.check_field_evidence_bundle()`。

### 将现场 bundle 接入 E7

生成 bundle 后可以把它作为可选、哈希绑定的顶层 `field_evidence_bundle` 写入四模式
清单。写入器和 `multidetect-release-gate` 都会重新打开 bundle 的七个输入并重算六层
摘要；只复制 `gate_passed` 或改写分层摘要不会形成通过结果。预发布可以绑定
`gate_passed=false` 的待办报告（用 `--allow-incomplete`），最终 `v0.3.0` 则要求该
记录存在、`real_domain_required=true` 且六层全部通过：

```powershell
$manifestOut = 'artifacts\evaluation\release-gate-pre.8-field-bound.json'
$bundle = 'artifacts\evaluation\field-evidence-bundle-<现场时间戳>.json'
& $py scripts\create_release_gate_manifest.py `
  --out $manifestOut `
  --field-evidence-bundle $bundle `
  --parity-json $parityFile `
  --replay $replayEvidence --sim $simEvidence --hil $hilEvidence --real $realEvidence `
  --replay-visual passed --sim-visual passed --hil-visual passed --real-visual passed `
  --replay-mission passed --sim-mission passed --hil-mission passed --real-mission passed `
  --replay-hardware not_required --sim-hardware not_required `
  --hil-hardware passed --real-hardware passed `
  --replay-real-control not_required --sim-real-control not_required `
  --hil-real-control passed --real-real-control passed `
  --allow-incomplete
& $py scripts\check_release_gate.py $manifestOut --report-only
```

清单 schema 的 `fieldEvidenceBundleRecord` 只保存 bundle 的路径、SHA-256、字节数和
六层/策略摘要；完整输入仍以 bundle JSON 为来源。该绑定提高现场证据的可追溯性，
不把报告的 `release_eligible=false`、`hardware_acceptance=not_claimed` 或软件/SIM
数据升级成 HIL、REAL、室外 GNSS 或人工 Fly View 验收。

2026-09-03 台式机复核实际通过 `-FieldEvidenceBundle` 生成了新的绑定清单
`artifacts/evaluation/release-gate-pre.8-current-20260902T213355Z-v6-bound.json` 和
report-only 结果 `...-report.json`。该清单同时绑定了新的
`field-link-preflight-20260903-desktop-recheck.json`；bundle 序列化摘要重验通过，
但 `hardware_link_ready=false`、六层 `gate_passed=false`，所以总体
`release_allowed=false`，旧清单和旧证据均未被覆盖。

## 运行

在仓库根目录执行：

```powershell
Set-Location D:\Projects\Multi-Detect
$env:PYTHONPATH = 'src;.'
$py = '.\ground-control\MultiDetectGCS\.venv\Scripts\python.exe'

# 只查看当前门状态，阻塞时仍返回 0，适合迁移/CI 诊断
& $py scripts\check_release_gate.py .\path\to\release-gate.json `
  --report-only `
  --out .\artifacts\evaluation\release-gate-report.json

# 发布前严格模式：只有 release_allowed=true 才返回 0
& $py scripts\check_release_gate.py .\path\to\release-gate.json
```

当前产品清单是 `v0.3.0-pre.8`，因此即使四个证据门全部为 `passed`，报告中的
`gate_passed` 也只能表示“证据齐全”；`release_allowed` 仍会因为产品通道是
`prerelease` 而保持 `false`。切换到最终 `v0.3.0` 前必须重新运行本门，并确认没有
`not_claimed`、`pending` 或硬件缺口。

## 证据边界

该工具只聚合并校验已经生成的机器可读证据，不把 fixture、dry-run、Docker SITL、
源码测试或 EXE smoke 自动升级为真实 HIL/REAL、室外 GNSS、实飞或人工视觉结论。缺失
文件、SHA 不匹配、运行模式不匹配、parity 不一致或任一状态未通过都会保留具体原因，
并让严格模式以退出码 `2` 结束。

## v65 四模式人工视觉记录的门禁边界

`multidetect-qgc-visual-review` 现可生成并独立复验 source-bound 人工视觉记录：它绑定 exact
PID/HWND machine report、PNG、QGC EXE、当前 `ui_contract_hash`，并要求近地平线负控至少 5 帧
有检测但产生 0 个目标坐标。12 项人工 checklist、reviewer、UTC、decision 和固定 attestation 都会
进入内容摘要；任一 bound file 漂移都会使 verifier fail-closed。

每份记录必须显式声明 `runtime_mode` 并与 machine report 一致。E7 已提供可重复的
`--qgc-operator-visual-acceptance-evidence` 输入，manifest 固定按 replay/sim/hil/real 排序；evaluator
逐份重开记录和绑定源，验证记录 SHA/bytes、review digest、reviewer/time、machine run/report、截图、
QGC EXE 与 UI contract。最终 release 必须覆盖 4/4 模式，并拒绝重复 mode、重复 evidence、跨模式复用
machine report 或相同 screenshot。

当前没有 accepted record，故 E7 集合为 `present=false/passed=false`。v61 PNG 只属于 HIL 机器捕获，
不能重复用于 REPLAY/SIM/REAL。只有四模式各自完成独立捕获、人工点验和 verifier 后，才能关闭这项
最终发布门；内容摘要不等同密码学身份签名，也不提升硬件、HIL、REAL 或实飞状态。
