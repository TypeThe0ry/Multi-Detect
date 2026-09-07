# Multi-Detect 当前进度

最后更新：2026-09-08（v192 工作树）

## v192 本轮已收口（Windows field capture wrapper）

- 新增 [`scripts/run_px4_runtime_capture.ps1`](../scripts/run_px4_runtime_capture.ps1)：从脚本自身解析仓库根目录，
  自动选择 `ground-control/MultiDetectGCS/.venv`、仓库 `.venv` 或 PATH 中的 Python，并调用同一个
  `multidetect.px4_runtime_capture` 模块；现场不需要手抄长命令或依赖当前工作目录。
- wrapper 完整转发 `hil|real`、串口/UDP endpoint、baud、system/component ID、stale/duration/poll 边界、
  `AUTOPILOT_VERSION`/ranging 请求、三个 sidecar 输出路径和 `--overwrite`。REAL 必须给 signing identity
  hash 或 signing-key 环境变量名，HIL 明确拒绝两者；HMAC/signing 原文只由 Python 子进程读取环境变量，
  不进入 PowerShell 参数列表，也不改变网卡、路由或飞控状态。
- 增加 PowerShell parser、转发契约、密钥边界、模式 fail-closed 和 parity 断言；wrapper 与 capture 模块、
  schema、确定性 generator 一起纳入 PX4 Mission path hash。当前 v192 的现场入口仍只生成 sidecar 和软件
  join，不自动声称真实 Pixhawk、室外 GNSS、HIL/REAL、人工 Fly View、飞行或最终 `v0.3.0` 通过。
- v192 聚焦回归为 `97 tests / 96 passed / 1 skipped / 0 failed`，JUnit 位于
  `artifacts/evaluation/terminal-continuation-v192/px4-runtime-capture-wrapper-focused-v192.xml`，15,216 bytes /
  SHA-256 `D4C68D5C6139E4B0920FD911DDB39EDFD6E6CF78B08E712C0727A1C1CDA9B63C`；全量回归为
  `3,783 passed / 7 skipped / 0 failed`（3,790 tests，197.147 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v192/full-rerun-v192-final.xml`，555,245 bytes /
  SHA-256 `2C48EB74343AF0A8E5F7358CC1B94297426078FA5B8C51603E520A1B159A2BF9`。docs/parity、ruff、
  format、compileall、PowerShell parser 与 diff check 均通过。wrapper 变更后的当前 parity 为 10,690 bytes /
  SHA-256 `C4B0272564FE365AC95497D8D339C190549909EC37D4F20E4F3F5165BB4911F9`，PX4 Mission path hash 为
  `DA5C8EDF57A2F8A80867DEE4B85B4238916F563D171FCF1A0DC674CDB99B3AF8`。
- 现场调用模板（在仓库根目录 PowerShell）：
  `& .\scripts\run_px4_runtime_capture.ps1 -BaseDirectory <capture-root> -Mission mission.json
  -RuntimeObservation runtime-observation.json -RuntimeProfile signed-profile.json -RuntimeMode hil
  -Endpoint <serial-or-udp-endpoint> -RuntimeProfileKeyId <KEY_ID>`；先在当前进程设置
  `$env:MULTIDETECT_RUNTIME_PROFILE_HMAC_KEY`，不要把密钥写进命令历史。

## v191 本轮已收口（PX4 receive-only runtime sidecar capture）

- 新增 [`src/multidetect/px4_runtime_capture.py`](../src/multidetect/px4_runtime_capture.py) 与安装式命令
  `multidetect-px4-runtime-capture`：在同一个 bounded Pixhawk read-only provider session 中采集 v2
  `PX4 Mission feedback`、endpoint/vehicle identity sidecar，并立即重开并重跑 v190 的 live observation /
  Mission / signed RuntimeProfile 五源 join。输入和输出均要求位于同一 `--base-directory`，拒绝符号链接、越界
  路径、输入/输出复用和 profile/runtime mode 漂移；所有 JSON 原子写入。
- 传输计数从兼容性的 `messages_transmitted` 总数拆为
  `telemetry_request_messages_transmitted`、`application_messages_transmitted` 和
  `flight_control_messages_transmitted`。`AUTOPILOT_VERSION` 等有界身份请求可被记录而不会被误判为飞控输出；
  任一应用或飞控输出仍会让 feedback/profile/join 门 fail-closed。旧 v1/v2 报告没有分桶字段时继续按保守规则
  将非零总数视为应用输出。
- `--request-autopilot-version`（默认开启）让现场采集可拿到完整 PX4 firmware identity；REAL 模式仍要求调用者
  通过环境变量或内存参数提供 signing identity hash，HMAC/profile key 只从环境或调用者内存读取，绝不写入报告。
  现场入口示例：
  `multidetect-px4-runtime-capture --base-directory <capture-root> --mission <mission.json>
  --runtime-observation <live-observation.json> --runtime-profile <signed-profile.json> --runtime-mode hil
  --endpoint <serial-or-udp-endpoint> --runtime-profile-hmac-key-env <KEY_ENV>
  --runtime-profile-key-id <KEY_ID> --request-autopilot-version`。
- 新增 [`configs/schemas/px4-runtime-capture.schema.json`](../configs/schemas/px4-runtime-capture.schema.json)
  和 [`scripts/generate_px4_runtime_capture_evidence.py`](../scripts/generate_px4_runtime_capture_evidence.py)。
  v191 确定性证据在 `artifacts/evaluation/terminal-continuation-v191/summary-v191.json`：正向 receive-only capture
  的 feedback/join/schema 均通过，telemetry 请求 `2`、应用输出 `0`、飞控输出 `0`；负向应用输出样本被 feedback
  与 join 同时拒绝。该证据使用 bounded in-memory provider，不连接真实 Pixhawk、串口、GNSS 或飞机。
- v191 聚焦回归为 `54 tests / 53 passed / 1 skipped / 0 failed`；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v191/px4-runtime-capture-focused-v191.xml`，9,118 bytes / SHA-256
  `CB77D5255B3708B600B63339D876BAE0DB9127E0A34F0CBB96D9BA6813A04307`。`ruff check`、`ruff format --check`、
  `compileall` 与 `git diff --check` 均通过；完整回归为 `3,779 passed / 7 skipped / 0 failed`
  （3,786 tests，187.779 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v191/full-rerun-v191-final.xml`，554,650 bytes / SHA-256
  `CD69D10146C6A361CF25DDD5905A13F9AFB640DA44999F36E8861A993322DBA9`。当前 parity 为 10,645 bytes / SHA-256
  `7BB1D0252C93A5177E0380E219CB285F2FF123746FEE71DB358399B657840787`，PX4 Mission path hash 为
  `1758A3A6AC52C01260E432ADC005E5AC9993978359F7243A281A02645B98DD97`。
- v191 当时只关闭“现场采集入口缺失”和“telemetry 请求误判为输出”这两个软件缺口；不把合成 provider 升级为真实
  HIL/REAL、室外 GNSS、人工 Fly View、飞行或最终 `v0.3.0` 发布通过。下一步是把现场生成的 sidecar 原样带回，
  用同一 CLI 重跑 join，再执行三机型 Mission feedback、GNSS/测距和人工视觉验收门。

## v190 本轮已收口（live RuntimeObservation × PX4 Mission × RuntimeProfile × endpoint/vehicle identity join）

- 新增 [`src/multidetect/live_runtime_observation_mission_binding.py`](../src/multidetect/live_runtime_observation_mission_binding.py)：
  以同一 `base_directory` 显式重开并哈希 live `RuntimeObservationResult`（含嵌套
  `RuntimeRunReport`）、可执行 `PX4 Mission`、v2 `PX4 Mission feedback`、签名 `RuntimeProfile` 和
  `runtime-endpoint-vehicle-identity` sidecar；拒绝符号链接、越界路径、空/超大/重复 source、模式/哈希/
  heartbeat/endpoint/airframe 漂移和任一输出解锁。HIL/REAL 的 profile HMAC 只从调用者内存或环境变量读取，
  报告不落密钥；身份 sidecar 固定 receive-only、无飞控输出、`output_locked=true`。
- 新增安装式命令 `multidetect-live-runtime-observation-mission-bind`，成功和错误输出分别由
  [`live-runtime-observation-mission-binding.schema.json`](../configs/schemas/live-runtime-observation-mission-binding.schema.json)
  约束；endpoint/vehicle 身份 sidecar 由
  [`runtime-endpoint-vehicle-identity.schema.json`](../configs/schemas/runtime-endpoint-vehicle-identity.schema.json)
  约束。`src/multidetect/release_parity.py` 已将 joiner、证据生成器和两个 schema 纳入 PX4 Mission path。
- [`scripts/generate_live_runtime_observation_mission_binding_evidence.py`](../scripts/generate_live_runtime_observation_mission_binding_evidence.py)
  用真实 `LiveMissionRunner.from_runtime_backend` 两帧 bounded HIL producer、同版签名 profile、可执行 Mission、
  v2 feedback 和 identity sidecar 生成可重开软件证据：join 报告 6,340 bytes / SHA-256
  `CDF38A290AF39986BA64DB4791349693D7F02B1E88F351951711386C54951C42`，25 项检查全部通过，schema errors `0`；
  负向模式篡改被拒绝（identity sidecar 的 REAL signing identity 缺失、模式不一致和 output lock 均关闭 gate）。
  五个 join source 为 live observation 1,403 bytes / `1659D846E6AA3C5AD0441FB1772202DBCE6247C06D994A16E3765C4E60138BE0`、Mission 795 bytes /
  `9FA908D84C5C9BF85330C5CC4AF983CEFE133B5FF2837C48EE243FD19DE5F70C`、feedback 4,254 bytes /
  `06D613785990F03E16D9BEB0FF254EBA34D45C2B292500608E66B36F91B76815`、profile 727 bytes /
  `00969A4FE6DF6ACB66C2F20BFD02AE4B81295B920A0D129775665B380E7BC416` 和 identity 1,470 bytes /
  `4F51C73A24E899D5B0296B59BD4E26B5081FAE94B20CA2A83BD8F87F2DDF82C2`；live 内嵌 RuntimeReport 536 bytes /
  `4D74129933FDA1086361A56438496221741C17637BB54E0FA174DC63EA02FA3` 作为同源嵌套证据。
- v190 定向回归为 `145 passed / 2 skipped / 0 failed`；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v190/live-runtime-observation-mission-binding-focused-v190.xml`，
  20,037 bytes / SHA-256 `3B029752BCDAB4AFC4F63BCDC84A5C348A0393D671B16C82CDEC7704502B38D3`。
- 全量 Python 回归为 `3,770 passed / 7 skipped / 0 failed`（3,777 tests，180.87 秒）；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v190/full-rerun-v190-final.xml`，553,421 bytes / SHA-256
  `66FA6C5AA8CE96F3BC1755D452071AB9C206DDC9DA16F5C33DCB7BECB7B3A95E`。
  v190 evidence 当时绑定的 `configs/release-parity.json` 为 10,482 bytes / SHA-256
  `98177D06A88565D5FFDCE97F265CFF573CCC1256F5DE4A64C737DAEE72BE8435`，PX4 Mission path hash 为
  `7C4DDE6A78ECE45886F72AD8F28815B275C2614089C109FCEFEE095AD894EA6D`。
  本轮只证明 software sidecar join、schema、hash/身份/锁输出和 fail-closed 逻辑；不把合成 HIL 证据升级为
  真实 Pixhawk、室外 GNSS、HIL/REAL、人工 Fly View、飞行或最终 `v0.3.0` 发布通过。下一步是用真实设备采集
  同版 profile/Mission/feedback/endpoint sidecar 并重跑此 join，再继续执行现场验收门。

## v189 本轮已收口（profile-bound live RuntimeObservation producer）

- `LiveMissionRunner.from_runtime_backend` 现在直接拥有 live producer 的完成报告和类型化观测
  结果入口：`runtime_run_report` 记录同一 descriptor/input hash、实际处理帧 ID、共享时钟起止时间、
  `stream_exhausted` 与 `max_frames`；`build_runtime_observation_result()` 在 HIL/REAL 模式下从该报告
  派生 canonical receive-only result。无界 live 流被用户中止时不伪造终止原因，报告保持不可用。
- `multidetect live-camera` 增加 `--runtime-report`、`--runtime-observation-result` 和
  `--runtime-observation-include-hash`。输出要求签名 profile-bound HIL/REAL runtime；先构造并校验
  observation，再原子写入 result/report，避免安全门失败留下孤立 sidecar。
- live producer 暴露分离后的计数：`messages_transmitted` 是兼容性的总数，
  `telemetry_request_messages_transmitted` 单列 telemetry/autopilot/stream 请求，
  `application_messages_transmitted` 与 `flight_control_messages_transmitted` 单列应用/飞控输出。
  Pixhawk read-only provider 的 telemetry 请求不会被误报为零发送；观测结果构造器拒绝任何已知应用/飞控
  输出、可控硬件或飞控能力开启的 provider。
- 新增 [`scripts/generate_runtime_observation_live_producer_evidence.py`](../scripts/generate_runtime_observation_live_producer_evidence.py)：
  用真实 `LiveMissionRunner.from_runtime_backend` 路径生成 HIL 软件 pair。结果 1,419 bytes / SHA-256
  `2FADB5B4D6A5E041DBE35430EE03E4CB7549955B2F757F50F9FE243A87DBC01B`；RuntimeReport 544 bytes /
  SHA-256 `F4D7B8E2E61AA2B7972F38BBEBA9F8857424116316F7DFB83565B5CDDCB5CFE2`；schema errors `0`、
  round-trip `true`、binding `gate_passed=true`，负向应用输出 guard 按预期拒绝。Telemetry 总数为 `2`，
  其中请求 `2`、应用 `0`、飞控 `0`，`read_only=true`、`output_locked=true`。
- 聚焦回归为 `307 tests / 306 passed / 1 skipped / 0 failed`；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v189/runtime-observation-live-producer-focused-v189.xml`，
  41,264 bytes、SHA-256 `07BF0AEE56F35D75389D310C9A36ACDDD676CC114055E6E6CA043E48A496160A`。
  全量回归为 `3,770 tests / 3,764 passed / 6 skipped / 0 failed`，365.571 秒；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v189/full-rerun-v189-final.xml`，552,187 bytes、SHA-256
  `691416301B89CBDFBBC91EC3617AE67AB9B37B07242C68CD3241684A51D6922A`。
- 本轮已更新 release parity（PX4 Mission path 与 safety policy hash）并提交实现；仍未重编译 portable EXE。
  这些是 software runtime identity/安全边界证据，不代表真实 Pixhawk、室外 GNSS、HIL、REAL、人工 QGC
  或实飞验收；最终 `v0.3.0` 继续保持阻断。下一步是把 live 结果与同版 RuntimeProfile、PX4 Mission
  feedback、endpoint/vehicle identity sidecar 做显式 join。

## v188 本轮已收口（RuntimeObservation builder CLI）

- 新增安装式命令 `multidetect-runtime-observation-build`（实现入口
  `src/multidetect/runtime_observation.py:main`）：输入现有 `RuntimeRunReport` sidecar、指定
  `--runtime-mode hil|real` 和 `--out`，即可生成与 sidecar 同源的 canonical receive-only result；
  `--include-hash` 可额外写入 result `sha256`，不会改写原 sidecar。
- CLI 对错误路径保持非零退出，并通过同一 `load_runtime_observation_result` 严格重开；它只构造
  `RuntimeObservationResult`，不连接 Pixhawk、不发送 telemetry/control 消息，也不把 covariance
  capture 的 telemetry request 计数转换成零发送声明。
- 新增 [`scripts/generate_runtime_observation_cli_evidence.py`](../scripts/generate_runtime_observation_cli_evidence.py)：
  v188 HIL/REAL 两个 CLI pair schema errors 均为 `0`、重开为 `true`、binding `gate_passed=true`。
  HIL result 1,392 bytes / SHA-256 `C344CDB4A31A71841AA512A69CE40DDBC4092BEC96BFFE8A601485B00B27535D`；
  REAL result 1,398 bytes / SHA-256 `EB6E7A23F86108696BD0BC7826D0C4BB4A07C49A5B75D74490F30F25FE4069A4`。
- 定向 `tests/test_runtime_observation.py tests/test_runtime_report_binding.py tests/test_runtime_report_binding_set.py`
  为 `39 tests / 38 passed / 1 skipped / 0 failed`；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v188/runtime-observation-cli-focused-v188.xml`，6,274 bytes、SHA-256
  `F82133782267F4556DF51EFC5E742C5871740C36A2331CF12F98DCDBC0C53DFD`。本轮仍不改变 portable EXE、
  硬件、室外 GNSS、真实 HIL/REAL、人工 Fly View、实飞或最终 `v0.3.0` 发布状态。

## v187 本轮已收口（typed RuntimeObservationResult 生产入口）

- 新增 [`src/multidetect/runtime_observation.py`](../src/multidetect/runtime_observation.py)：以同一份
  `RuntimeRunReport` 为唯一来源，类型化生成 `hil_runtime_observation_finished` /
  `real_runtime_observation_finished` 结果；descriptor/input hash、处理帧 ID、数量、终止字段和内嵌
  canonical report 不再由现场 producer 手工复制。`write_runtime_observation_pair` 同时写 result 与
  sidecar，`load_runtime_observation_result` 会严格重开并拒绝身份、报告或安全字段漂移。
- receive-only 字段由 builder 固定为 `observation_only/read_only/receive_only=true`、
  `messages_transmitted=0`、`flight_control_enabled=false`、`hardware_outputs_present=false`、
  `output_locked=true`；明确区分 telemetry-request capture 与零应用发送的观测结果，避免把现有
  Pixhawk covariance 采集器的 telemetry 请求数误记成控制输出或反向冒充现场验收。
- 新增 [`configs/schemas/runtime-observation-result.schema.json`](../configs/schemas/runtime-observation-result.schema.json)、
  [`scripts/generate_runtime_observation_builder_evidence.py`](../scripts/generate_runtime_observation_builder_evidence.py)
  和 `tests/test_runtime_observation.py`。v187 HIL/REAL 两个软件 pair 均 schema errors `0`、binding `gate_passed=true`；
  篡改 `runtime_input_hash` 的负向 pair `gate_passed=false`，失败检查为 `input_hash_matches`。证据摘要位于
  `artifacts/evaluation/terminal-continuation-v187/summary-v187.json`。
- 定向 `tests/test_runtime_observation.py tests/test_runtime_report_binding.py tests/test_runtime_report_binding_set.py`
  为 `38 tests / 37 passed / 1 skipped / 0 failed`；JUnit 位于
  `artifacts/evaluation/terminal-continuation-v187/runtime-observation-focused-v187.xml`，6,139 bytes、SHA-256
  `851CC04C0C75C00CE0C17815994FB1463D4EE455C82AD0DE5486A4552D2CC4DE`。本轮仍为 Python/schema/docs-only，
  全量回归为 `3,760 passed, 6 skipped, 0 failed`（3,766 tests，293.085 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v187/full-rerun-v187-final.xml`，551,664 bytes、SHA-256
  `9EC0FC399BCA4422390D2A5E5BD89ED08FB275E82286BF6503A53EFC76856BC5`。本轮仍为 Python/schema/docs-only，
  portable EXE 未重编译；硬件、室外 GNSS、真实 HIL/REAL、人工 Fly View、实飞和最终 `v0.3.0` 继续保持
  `not_claimed` / 阻断。

## v186 本轮已收口（HIL/REAL RuntimeReport 观测适配器）

- `runtime_report_binding` 新增两个严格事件适配器：`hil_runtime_observation_finished` 和
  `real_runtime_observation_finished`。它们按事件固定 runtime mode，重新读取 descriptor/input hash、
  processed frame count/IDs、bounded termination，并要求结果内嵌的 `RuntimeRunReport` 与 sidecar canonical
  mapping 完全一致；未知事件仍保持 fail-closed。
- HIL/REAL 观测结果必须显式声明 `observation_only/read_only/receive_only=true`、
  `messages_transmitted=0`、`flight_control_enabled=false`、`hardware_outputs_present=false`、
  `output_locked=true`。新增 `output_safety_contract` 单绑定检查并同步两个 binding schema；发送计数、
  事件/模式漂移、缺少 `max_frames` 或内嵌报告都会关闭 gate。
- 新增 [`scripts/generate_runtime_report_binding_observation_evidence.py`](../scripts/generate_runtime_report_binding_observation_evidence.py)，
  在 `artifacts/evaluation/terminal-continuation-v186/` 生成四模式 `4/4` 软件绑定、REPLAY/SIM 子集和
  `messages_transmitted=1` 负向报告：正向集合 14,785 bytes / SHA-256
  `34F3B877B6C6DC77A751C1064AB32EA2F5AA2AF0DA6A4AC5C5307946B4A7273D`，schema errors `0`；负向集合
  `gate_passed=false`、`output_safety_contract=false`，schema errors `0`。
- 定向 `tests/test_runtime_report_binding.py tests/test_runtime_report_binding_set.py` 为 `28 passed, 1 skipped, 0 failed`；JUnit
  位于 `artifacts/evaluation/terminal-continuation-v186/runtime-binding-focused-v186.xml`，4,929 bytes、SHA-256
  `6C89C4A23DE25B928D0B1F2C8059BC69CFE0F7EDD597CBDA10DFF63709344834`。全量回归为 `3,751 passed, 6 skipped, 0 failed`
  （3,757 tests，342.241 秒），JUnit 位于 `artifacts/evaluation/terminal-continuation-v186/full-rerun-v186-final.xml`，
  550,454 bytes、SHA-256 `F2EAC63E24F34B2A9B34A3D1F7C48E32636F9360FB6E997CCA7E1480F915A02B`。
- 本轮为 Python/schema/docs-only，未重编译 portable EXE。合成 HIL/REAL 只验证接口和安全合同，
  `hardware/HIL/REAL/flight/manual visual` 仍 `not_claimed`，E7 与最终 `v0.3.0` 继续阻断。

## v185 本轮已收口（RuntimeProfile → PX4 Mission feedback 六源绑定）

- 新增 [`src/multidetect/px4_mission_feedback_profile_binding.py`](../src/multidetect/px4_mission_feedback_profile_binding.py)
  与 `multidetect-px4-mission-feedback-profile-bind` CLI：显式重开 v184 RuntimeProfile sidecar 集合和
  `px4_mission_feedback_binding` 六源报告，按 `REPLAY → SIM → HIL → REAL` 模式策略对账，再逐叶检查
  `HIL/REAL × fixed-wing/multirotor/VTOL` 的 profile kind、签名 key、runtime binding、receive-only 与锁输出。
- 新增 [`px4-mission-feedback-profile-binding.schema.json`](../configs/schemas/px4-mission-feedback-profile-binding.schema.json)，
  成功与结构错误均有固定 schema；输出保留 profile set/feedback source bytes+SHA-256、模式策略、六个叶子结果、
  reasons 和 software-only claim boundary。profile set 的通用模式 sidecar 不被错误地当作六个空军架构文件，
  只绑定策略身份；六源 reviewer 继续负责每个 airframe 的实际 profile/mission/report 哈希与身份。
- 4 项定向回归覆盖六叶成功绑定、profile set 不完整、叶子 signer key 漂移的 fail-closed 以及 CLI 错误 schema；相关
  25 项回归为 `24 passed, 1 skipped`，清理后再次全量为 `3744 passed, 6 skipped, 0 failed`（3,750 tests，337.783 秒）。本轮仍不启动
  runtime、不连接 PX4、不改变 QGC C++/QML 和 E7，硬件、室外 GNSS、HIL/REAL、人工 Fly View 与实飞验收仍保持原门禁。

## v184 本轮已收口（RuntimeProfile sidecar 集合预检）

- 新增 [`src/multidetect/runtime_profile_binding_set.py`](../src/multidetect/runtime_profile_binding_set.py) 与
  `multidetect-runtime-profile-set-verify` CLI：显式重开每个 `REPLAY / SIM / HIL / REAL` 的 RuntimeProfile
  JSON sidecar，按固定模式顺序检查 profile kind、签名策略、`key_id`、观察到的 endpoint/vehicle/firmware/
  airframe/signing identity，以及 `flight_control_enabled=false` / `output_locked=true`。
- HIL/REAL 的 HMAC key 只从调用者内存或环境变量读取，不写入输出；路径必须在 `base_directory` 内且拒绝符号链接、
  空文件、重复 mode 和重复 profile source。集合门允许用 `required_modes` 做 REPLAY/SIM 软件子集预检，默认仍要求
  四模式；缺少 HIL/REAL 时 `complete=false`，不会将 profile 身份记录升级为现场、硬件、HIL/REAL 或飞行验收。
- 新增 `runtime-profile-binding-set.schema.json` 和 9 项定向回归；完整四模式合成 profile 仅证明软件签名/身份/锁输出
  契约，保留 [`px4_mission_feedback_binding.py`](../src/multidetect/px4_mission_feedback_binding.py) 的六源现场门与
  E7 `false / false`。定向为 `8 passed, 1 skipped, 0 failed`；全量为 `3740 passed, 6 skipped, 0 failed`
  （3,746 tests，317.402 秒），JUnit 位于 `artifacts/evaluation/terminal-continuation-v184/`。Python-only 改动未重编译
  portable EXE。

## v183 本轮已收口（多模式 RuntimeReportBinding 集合校验）

- 新增 [`src/multidetect/runtime_report_binding_set.py`](../src/multidetect/runtime_report_binding_set.py) 与
  `multidetect-runtime-report-set-verify` CLI：把每个运行模式的结果 JSON 与 `RuntimeRunReport` sidecar
  作为显式 pair 重新打开并绑定，固定 `REPLAY → SIM → HIL → REAL` 顺序，拒绝重复模式、跨模式复用、
  base directory 外路径、未知字段和任一单项错误。每条 pair 复用 13 项单绑定检查，集合层再检查模式覆盖、
  声明一致性、结果/报告路径唯一性、source 唯一性和全体通过条件。
- `configs/schemas/runtime-report-binding-set.schema.json` 同时约束成功与结构错误事件。v183 软件证据以
  2/2 `REPLAY + SIM` pair 通过；集合报告 8,035 bytes / SHA-256
  `52C6DAC94B3529C1C25782280D1A94DA6E17CC098AD7A494B0C73604B6B0144C`，schema errors 为 0；定向回归
  `8 passed, 1 skipped, 0 failed`（9 tests；Windows symlink 权限导致的预期 skip），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v183/runtime-binding-set-focused-v183.xml`，2,232 bytes / SHA-256
  `44A5EF5766847D1504CD3AAC69C7C5F0E93E6D81A2F9373149EFA27C6E235D9A`；全量 Python 回归为
  `3732 passed, 5 skipped, 0 failed`（3,737 tests，279.743 秒），JUnit 546,831 bytes / SHA-256
  `915357977330EF96CD990DA87AF134A1130997FA6F6204D9345AB798947C471B`。
- 默认四模式集合仍明确保持 `gate_passed=false`，缺少 `HIL` 与 `REAL`；集合校验只证明 software runtime
  identity，不把 Replay/SIM 证据冒充 HIL、REAL、硬件或飞行验收。Python-only 改动未重编译 portable EXE，
  当前可执行文件与 v180 绑定身份保持不变；E7 仍为 `false / false`。

## v182 本轮已收口（绑定器错误输出 schema 化）

- `multidetect-runtime-report-verify` 在输入 JSON 损坏、报告无法读取或契约异常时，现在输出完整的
  `runtime_report_binding_error` 文档：13 项检查统一为 `false`，`result/runtime_report/expectations` 明确为 `null`，
  并保留结构化 `error.type/message` 与稳定 `reasons`；退出码仍为 `1`，不会把失败当成通过。
- `configs/schemas/runtime-report-binding.schema.json` 现在同时约束成功和失败两种输出，失败路径也可由同一
  JSON Schema 校验；损坏 JSON fixture 的 schema 校验通过，绑定门保持 `false`。
- v182 错误证据位于 `artifacts/evaluation/terminal-continuation-v182/`：输入 11 bytes / SHA-256
  `2F021715A3272E09152B7FDD6ABCA9A71CD53F67389855C522BFA4D754D9EE08`；错误报告 1,201 bytes / SHA-256
  `29E7923EE8F20C6617E08538E0962F4C0A84F442AA599510401A3ECA7C3AEC15`，schema errors 为 0。
- 定向回归为 `44 passed, 0 skipped, 0 failed`，JUnit 位于
  `artifacts/evaluation/terminal-continuation-v182/runtime-binding-focused-v182.xml`，6,452 bytes，SHA-256
  `F431DD257A58DA62B6CE5D74529FB0867C0647A4D5FAFBD30B439FD853DF5191`；全量 Python 回归为
  `3724 passed, 4 skipped, 0 failed`（3,728 tests，385.28 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v182/full-rerun.xml`，544,825 bytes，SHA-256
  `3B104184FFDCC0F2611BF369BCA6DDC0B3D2C50F8E61453786857819F3C57702`。本轮仍只增强 software 证据输出；E7、
  室外 GNSS、硬件、HIL/REAL、人工 Fly View 与实飞门保持 `false / false`。

## v181 本轮已收口（真实 integrated Replay 结果格式兼容绑定）

- `runtime_report_binding` 现在同时读取当前生产结果的嵌套 `result.processed_frames`/报告门，和历史
  integrated Replay 产物的顶层 `processed_frames`、`frame_ids`、`natural_stream_exhaustion` 形状；两种形状都必须
  重新绑定 descriptor/input hash、处理帧数量/ID、自然耗尽和无飞控/硬件输出边界，缺字段仍 fail-closed。
- 帧 ID 字段不再静默过滤非字符串、空值或重复值；一旦结果声明了 malformed ID，绑定报告直接失败，避免旧产物
  被“部分解析后通过”。`integrated_replay_finished` 与 `integrated_replay_qgc_finished` 共用同一适配器。
- v181 历史 2 帧 integrated Replay 证据绑定通过：结果 1,276 bytes / SHA-256
  `9A5A8827D9891DC33E763CD6EEA2E904AC61C9C91B02F9772C3A87791C1B7588`；RuntimeRunReport 507 bytes / SHA-256
  `8B6DF9AD8106EA9A83A49BD5C1D9281EA24604F7969649141001A7FDE8433906`；绑定报告 2,410 bytes / SHA-256
  `5CA09D94B667388A537121C3DFCD9F7988A2EDDD31C425AE3F466819D608D33C`，`gate_passed=true`。
- 定向回归为 `43 passed, 0 skipped, 0 failed`，JUnit 位于
  `artifacts/evaluation/terminal-continuation-v181/integrated-runtime-binding-focused-v181.xml`，6,332 bytes，
  SHA-256 `7B8C379AD496BDC0952A238C479F6197F7298B26496A93A19113855A0993C437`；全量 Python 回归为
  `3723 passed, 4 skipped, 0 failed`（3,727 tests，306.38 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v181/full-rerun.xml`，544,704 bytes，SHA-256
  `76008B1C63BD8175159289646ACC70251B9DF89D4417DB11CF4C415357D1EC67`。本轮仍只锁定 software 证据身份；E7、
  室外 GNSS、硬件、HIL/REAL、人工 Fly View 与实飞门保持 `false / false`。

## v180 本轮已收口（运行结果与 RuntimeRunReport 独立绑定）

- 新增 [`src/multidetect/runtime_report_binding.py`](../src/multidetect/runtime_report_binding.py) 与
  `multidetect-runtime-report-verify` CLI：重新打开结果 JSON 和 `RuntimeRunReport` sidecar，拒绝重复键、软链接、
  非对象和损坏/篡改报告，并按已知事件严格核对 runtime mode、descriptor/input hash、处理帧数量/ID、终止语义、
  integrated Replay/QGC 内嵌报告以及 SIM 的 `truth_free`/`closed` 状态；未知事件保持 fail-closed。
- `SimulatorScenarioRunResult.to_mapping()` 现在公开生成 sidecar 所使用的精确 `runtime_input_hash`，审核者不需要
  重建场景输入即可把结果和报告绑定。该字段只扩展 SIM software 结果，不改变 `px4_mission_path` parity、QGC C++/QML
  二进制或任何飞控输出路径。
- 真实 2 帧 `river_rescue_vtol_001` SIM 证据绑定通过：结果 17,161 bytes / SHA-256
  `3883CED727A1ED73F09F63FFC222808406EFB8B46A93B44810385DE136604195`；RuntimeRunReport 542 bytes / SHA-256
  `BF6904C29DC673BFB2BE70C6BA4FFEF8378F240BF553B628BA2545ABB42E304E`，canonical SHA-256
  `31B18485D212AF0ABE04C1C9DDE1F6F1CE96F5CA6C0985E64A69D3A78A1A4A7E`；绑定报告 2,431 bytes / SHA-256
  `7E5C96466580E36BD038199D1B23FD8C7852B3C758710AA4328FE15705109A9B`，`gate_passed=true`。
- v180 定向回归为 `41 passed, 0 skipped, 0 failed`，JUnit 位于
  `artifacts/evaluation/terminal-continuation-v180/runtime-report-binding-focused.xml`，6,062 bytes，SHA-256
  `0FCA9E28E44C47FE8BC42B9B0D3A9CC5F874B325B37584DE14ECF7975063B493`；全量 Python 回归为
  `3721 passed, 4 skipped, 0 failed`（3,725 tests，311.097 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v180/full-rerun.xml`，544,434 bytes，SHA-256
  `D6D12065420C8C64CE91379FBB5411E7DC7DA3F8EB5C9036207807DFE1FE575C`。这轮先锁定 software 证据身份；硬件、
  室外 GNSS、HIL/REAL、人工 Fly View 与实飞门仍未通过，不改变 E7 `false / false`。

## v179 本轮已收口（integrated Replay/QGC sidecar 对账）

- `integrated-replay-qgc` 的 CLI 回归现在挂接真实 `RuntimeRunReport`，在本地 RTSP/metadata 同步 ACK 完成后
  写出 `--runtime-report` sidecar，并核对结果内嵌报告、canonical hash 与 loader round-trip；QGC 入口不再只是
  解析参数，而是覆盖完整报告消费路径。
- 报告仍绑定 `frame-0001`/`frame-0002`、descriptor/input hash、`stream_exhausted=true`、`max_frames=null`；
  integrated Replay、QGC、Live、Runtime 定向回归为 `140 passed, 0 skipped, 0 failed`。
- 定向 JUnit 位于 `artifacts/evaluation/terminal-continuation-v179/integrated-replay-qgc-focused.xml`，19,230 bytes，
  SHA-256 `B4B6620511FE02F01D07177695CFFA28124E18A2502802AE9D7539E4FB813F88`；v179 全量 Python 回归为
  `3711 passed, 4 skipped, 0 failed`（3,715 tests，269.676 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v179/full-rerun.xml`，543,119 bytes，SHA-256
  `EDF8454297EF15BDA74213EDF5DD18C9110C9A4E8A61D6E1A4A998AD3F3DFFAD`。这是 QGC 入口补强，不改变
  QGC C++/QML 二进制，也不提升室外 GNSS、真实硬件、HIL/REAL、人工视觉或实飞门。

## v178 本轮已收口（integrated Replay 自然耗尽与 RuntimeRunReport）

- `ReplayFrameSource` 为有限 recording 增加独立的 `FrameSourceExhausted` 结束信号；`LiveMissionRunner`
  将其记录为 `camera.stream_exhausted`，保留直接 `read()` 的历史 `RuntimeError` 兼容性，避免把自然结束和
  相机读取故障混在一起。
- `IntegratedReplaySession` 现在只接受完整 recording：逐帧核对实际消费 ID 顺序、frame count、descriptor/input
  hash 以及 source exhaustion，再生成与 SIM/HIL/REAL 共用的 hash-bound `RuntimeRunReport`。兼容传入的
  `max_frames == frame_count` 会归一化为 `null`，报告明确 `complete=true`、`termination_reason=stream_exhausted`。
- `integrated-replay` 与 `integrated-replay-qgc` 支持 `--runtime-report <path>`，结果 JSON 嵌入同一 canonical
  report，sidecar 原子写入并通过 loader round-trip；v178 定向回归为 `140 passed, 0 skipped, 0 failed`。
- v178 证据位于 `artifacts/evaluation/terminal-continuation-v178/`：runtime report 507 bytes，SHA-256
  `8B6DF9AD8106EA9A83A49BD5C1D9281EA24604F7969649141001A7FDE8433906`，canonical SHA-256
  `F72FBCEE791A97DBAA701B5FAC2C25FA9535359D61A575532B7BF49EAE93CF78`；run summary 1,276 bytes，SHA-256
  `9A5A8827D9891DC33E763CD6EEA2E904AC61C9C91B02F9772C3A87791C1B7588`；focused JUnit 19,230 bytes，SHA-256
  `6CB22CBD107BF23E0191801DE02AFB215A328F324726721A0DCBCF4A043A4C76`。
- v178 全量 Python 回归为 `3711 passed, 4 skipped, 0 failed`（3,715 tests，236.371 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v178/full-rerun.xml`，543,117 bytes，SHA-256
  `736FBAB2D45161703BDC256C5758D636EDEA33CDB2C0DE7E536E3FCD8D052A3B`；该轮只增强 software/Replay/QGC
  证据链，不改变 QGC C++/QML 二进制，也不提升室外 GNSS、真实硬件、HIL/REAL、人工视觉或实飞门，E7 仍按
  report-only 规则阻断。

## v177 本轮已收口（SIM RuntimeRunReport sidecar 与 bounded-prefix 对账）

- `SimulatorScenarioRunResult.runtime_run_report` 复用 Replay/HIL/REAL 的统一 `RuntimeRunReport` 契约，绑定
  manifest runtime channel hash、SIM `RuntimeDescriptor`、稳定的 `sim:<scenario>:<index>` 帧 ID 和共享
  `ManualClock`；场景 evidence 的 `frame_budget_complete` 仍表示预算跑满，sidecar 则明确
  `stream_exhausted=false`、`termination_reason=frame_limit_reached`，避免有限 SIM 前缀被误报为自然完整流。
- `multidetect.simulator.scenario_runner` 新增 `--runtime-report <path>`，通过同一原子 writer 生成可独立加载的
  JSON sidecar。v177 CLI 产物为 3 帧 VTOL、`complete=false`、`max_frames=3`、报告 canonical hash
  `A8F02A03A8695484A1EF5759D360C908BDE49CC06264A12F050B1B4251BBF014`；报告文件位于
  `artifacts/evaluation/terminal-continuation-v177/simulator-runtime-report-v177.json`，574 bytes，SHA-256
  `11B084DAF84F9B7BF2861A253F6311D10EB051A576AA0D32BC0197F57DC32669`。
- 同一 CLI 输出的 scenario evidence 位于
  `artifacts/evaluation/terminal-continuation-v177/simulator-run-v177.json`，19,797 bytes，SHA-256
  `A786EF286E6F49A29E92B5035129E0A5F51827AD60CEEA3A7A57A4AD9A64F478`；stderr 为 0 bytes，报告加载复核通过。
- `tests/test_simulator_scenario_runner.py` 新增 sidecar 属性、持久化往返和 CLI 写入回归；v177 场景 runner 定向
  回归为 `31 passed, 0 skipped, 0 failed`，JUnit 为
  `artifacts/evaluation/terminal-continuation-v177/scenario-focused-rerun.xml`（4,747 bytes，SHA-256
  `D8B5660FE07718F15785631B527295788DAEEE7D788D77C3440B3F44DE8B1A89`）。该更新只增强 truth-free software/SIM 记录，不改变 QGC C++/QML
  二进制，也不提升室外 GNSS、真实硬件、HIL/REAL 或实飞门。
- v177 工作树全量回归为 `3710 passed, 4 skipped, 0 failed`（3,714 tests，233.629 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v177/full-rerun.xml`，542,975 bytes，SHA-256
  `6DC8B44644D659929D60162B506D2F178ED4694B7E148597902424F8B4F366F6`；Ruff、format、compileall 与
  `git diff --check` 作为同一收口门复核。

## v176 本轮已收口（运行时完成报告与确定性前缀阻断）

- `RuntimeRunReport` 现在把自然耗尽与有界前缀分开记录：`stream_exhausted=true` 才表示完整流，传入
  `max_frames` 的运行会保留已处理帧但明确标记 `complete=false`、`termination_reason=frame_limit_reached`；
  不再把“刚好处理到上限”误报为完整 Replay/SIM 运行。
- 新增 `configs/schemas/runtime-run-report.schema.json` 以及 `write_runtime_run_report()`/
  `load_runtime_run_report()`：报告使用 canonical JSON 与 SHA-256，原子写入，严格拒绝未知字段、重复 ID、
  派生计数/时长/终止原因篡改和错误的 persisted hash；descriptor hash 与 backend input hash 仍绑定在同一身份边界。
- `runtime.py`、schema、parity source list 与回归测试已更新；RuntimeBackend 专项回归覆盖完整流、有界前缀、
  持久化往返、schema 校验和 tamper fail-closed。
- 当前 `configs/release-parity.json` 为 10,185 bytes，SHA-256
  `C3F9362479B8DF39C7126CDA5B84A573E86D907079391DFC82A505650D90F42B`；`px4_mission_path_hash` 为
  `727CDF63FC97FA8DA5D642274620FF3D6009C406912FF9B9A0D40E3685132CB8`。三机型 external join/revalidation 与
  PX4 response matrix/campaign 均重新绑定并 `gate_passed=true`，report-only E7 位于
  `artifacts/evaluation/terminal-continuation-v176/release-gate/`，仍为 `gate_passed=false`、
  `release_allowed=false`、32 条详细 blocker。
- 完整 Python 回归为 `3708 passed, 4 skipped, 0 failed`（3,712 tests，228.68 秒），JUnit 位于
  `artifacts/evaluation/terminal-continuation-v176/full-rerun.xml`，542,691 bytes，SHA-256
  `6678934D4FFB91F0F9FA331D0F5E193424DAA68DDB6169ADCDE416103E408830`；Ruff、format、compileall 与
  `git diff --check` 继续作为收口门。
- 增量 Windows Release 构建已完成（Ninja 报告 `no work to do`）：Release 与 portable EXE 均为
  `46,875,136` bytes、SHA-256 `41EA7AAF830B406AADBCBB17BD7E5E1E22C1A08F20DCBA002885E0153CF6765B`；
  `MultiDetectTargetEventReplayStoreSelfTest.exe` 退出 0 并输出 `PASS`，portable
  `--simple-boot-test` 退出 0，stdout/stderr 均为 0 bytes（SHA-256 均为
  `E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855`）。
- 当前仍是 `v0.3.0-pre.8` prerelease。运行时报告和 software/SIM 测试不等同于室外 GNSS、真实硬件、HIL/REAL、
  人工 Fly View 或实飞验收；现场 portable 长回放对账仍是下一道人工门。

## v175 本轮已收口（全量摘要一键复制与 parity 重绑）

- Fly View 回放弹窗新增“复制全量摘要”：只在 native replay store 已加载且完整性校验通过时启用，复制
  bundle/session、开始时间、总事件/媒体数、可定位/有效测距/有媒体/唯一目标统计、捕获时长、媒体字节和完整性状态；
  复制仍是只读剪贴板操作，不落地图、不选目标、不创建 Mission、不发送飞控指令。
- v175 继承 native/Python 完整事件流质量统计与最多 512 条显示上限；QGC 静态契约与回放专项回归为
  `45 passed, 0 skipped, 0 failed`，portable offscreen `--simple-boot-test` 退出 0 且 stdout/stderr 均为 0 bytes；
  native replay self-test 通过。
- Release/portable EXE 已重链并部署到
  `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`：
  46,875,136 bytes，SHA-256 `41EA7AAF830B406AADBCBB17BD7E5E1E22C1A08F20DCBA002885E0153CF6765B`；
  Release 与 portable 文件字节一致。
- 当前 `configs/release-parity.json` 为 10,129 bytes，SHA-256
  `1C937FFDD4D0D59267A5DF79A240E8101431613CAD0BE17533B2FFF6ECC33EBC`；core hash 保持
  `155A5011AA27042D587DD50AE29236A02DB4E7F42864CCF2607CB21AAC626D0A`，UI hash 更新为
  `46629AC7EC0AF1DC03B9D4D01B5EC56F959CF6C2C5EC27E59EBF76CD1090ECB9`。
- v175 external join/revalidation、PX4 response matrix/campaign 均 `gate_passed=true`，文件分别为
  5,492/565/6,985/26,120 bytes，SHA-256 为 `CBFAE8D4A08A97A1007FBCAE3FDE6F82D94816A77878AF6C17E9C21BCC4FD6AD` /
  `EE2C4F7D032315027C109D60E83EF36C0FD8B15408F8A83A99A027A308B8F702` /
  `8960871D28A7934D27C11C19C9F3ECA5844EF89F2BDBEE49E5348BA6F2CC73E3` /
  `3999F21F379B0B1EF46972413564A6D025390068CCD53D8A61DB093A33A06139`；matrix/campaign canonical SHA 为
  `051C9FA04794AFD8A74F40AA5EBA638983129FD137B53B41B5DE9776EAEFD52B` /
  `86E6318530784A3770F8A4AEC6B2E1E6E5C8BCD0525BD87B201809E3C764B5EE`。
- report-only E7 已重算到 `artifacts/evaluation/terminal-continuation-v175/release-gate/`，manifest/report 为
  23,729/73,786 bytes，SHA-256 `4EF6E491199AADEF899D31DA3E02DAEFCC677A8C19CFAC58B62411B5226F5FF4` /
  `7941392E1A51C1119A9C7661A22ED23E3B64D4D5D852A213CE4F1C35B58062DA`；E7 仍为
  `gate_passed=false`、`release_allowed=false`、32 条详细 blocker（仓库基线保持 31）。
- 本轮没有提升室外 GNSS、真实硬件、HIL/REAL、人工 Fly View 或实飞状态；现场链路继续 fail-closed。
- 完整回归随后完成 `3704 passed, 4 skipped, 0 failed`（3,708 tests，195.301 秒），JUnit
  `artifacts/evaluation/target-event-replay-v175-full-rerun.xml` 为 542,180 bytes、SHA-256
  `233628DA0C949A2F84E0690FF293E9F642D7D33BB64785E51316AA031B332BBC`；Ruff、format、compileall 与
  `git diff --check` 通过。

## v174 本轮已收口（全量回放质量摘要与 parity 重绑）

- `TargetEventReplayBundle` 与原生 `MultiDetectTargetEventReplayStore` 现在都从完整、已验证的事件流计算质量摘要：可定位事件数、有效测距事件数、有媒体事件数、唯一目标数、首尾捕获时长和哈希绑定媒体字节数；QML 仍只显示最多 512 条事件，避免长回放被截断后误读总量。
- Fly View 回放筛选同时显示“匹配总数”和“当前显示数”，四种筛选（全部、可定位、有效测距、有媒体）均使用 native 全量计数；事件明细、复制证据、地图/任务/飞控边界保持 v172 不变。
- C++ self-test、Python/native 跨语言回放测试、QGC 静态契约测试、portable offscreen simple boot 均通过；v174 定向回归为
  `45 passed, 0 skipped, 0 failed`，完整回归为 `3704 passed, 4 skipped, 0 failed`（3,708 tests，181.948 秒），JUnit
  `artifacts/evaluation/target-event-replay-v174-full-rerun.xml` 为 542,180 bytes、SHA-256
  `F891C6155A01A275AE96372F2DF38487483A8FA4CFC57A339F1EDBA9D8AD9FAA`；Ruff、format、compileall 和
  `git diff --check` 通过。
- Release/portable EXE 已重链并部署到
  `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`：
  46,875,136 bytes，SHA-256 `4E468DD936CD7F938E7373B0500294B12DDA9E1942FA9D9412D8803AB49E457A`；native replay self-test 为
  209,408 bytes，SHA-256 `69A860CE4A5ED7A94E66849FA0FA0CB86C658CCF9CAC29FF7B7099910BAF1195`。
- 当前 `configs/release-parity.json` 为 10,129 bytes，SHA-256
  `01001D1DFB2B043D03DAAD50CC88DABC669B90831FDA760D08C25937FF20836D`；core hash 为
  `155A5011AA27042D587DD50AE29236A02DB4E7F42864CCF2607CB21AAC626D0A`，UI hash 为
  `1A7F75ECB38A9880F88046214E97A5DE5BDA78A7BA0494943604BE2105244C71`。
- v174 重新绑定的 external join/revalidation、PX4 response matrix/campaign 均 `gate_passed=true`，文件分别为
  5,492/565/6,985/26,120 bytes；同一 parity 下的 report-only E7 位于
  `artifacts/evaluation/terminal-continuation-v174/release-gate/`，manifest/report 为 23,729/73,786 bytes，
  SHA-256 `D6AB3FD2477408196A8AA304BEE6FC1AB74B45550373B807C46EB304BEB56228` /
  `B58787B85F0FA33B35EABFD32E01C62F4FD2D204E60482A07016F9FD491432E6`；E7 仍为
  `gate_passed=false`、`release_allowed=false`、32 条详细 blocker。
- 本轮没有提升室外 GNSS、真实硬件、HIL/REAL、人工 Fly View 或实飞状态；现场链路仍按 fail-closed 记录。

## v173 本轮已收口（当前 parity 重绑与 E7 report-only 重算）

- 以当前 `configs/release-parity.json`（10,129 bytes，SHA-256
  `A12A5490E73ED52266A44F447DFD87895CD837476895AFFF2EFD43CAAE09C827`，UI
  `82FA274FB167812803C39FF7E260ACF334BB903436D04BB5E653879D806C0FDC`）重新生成并独立复核三机型 external
  runtime/AI join、PX4 `GLOBAL_POSITION_INT` response matrix 与 repeatability/fault campaign；三份 gate 均通过。
- 新证据位于 `artifacts/evaluation/terminal-continuation-v172/`：external join/revalidation 为 5,492/565 bytes，
  PX4 matrix/campaign 为 6,985/26,120 bytes；对应 source SHA-256 和 matrix/campaign canonical SHA 已写入机器状态。
- report-only E7 已在同目录 `release-gate/` 重算：manifest/report 为 23,729/73,786 bytes，SHA-256
  `6E1D31983EF6C74155D9E6FD818BD86B2CDD7956C32D33A8000F0A3BAAEC8A52` /
  `1913AD13F27157434F0D71AC574E2E9F58090F233BE55F54088E93BCDA078234`；当前
  `gate_passed=false`、`release_allowed=false`、32 条详细 blocker（仓库基线仍为 31）。
- 本轮只是 parity/证据重绑，不改变 v172 的只读回放功能或任何硬件能力声明；现场网卡仍为 `192.168.1.13/24`，
  Jetson/GR01/相机未连通，室外 GNSS、真实硬件、HIL/REAL、人工 Fly View 和实飞状态继续未声明。

## v172 本轮已收口（回放证据明细、筛选与复制）

- 原生 `MultiDetectTargetEventReplayStore` 继续作为唯一读取入口：事件媒体摘要现在包含 `kind`、相对路径、
  encoding、bytes、完整 SHA-256 与可选宽高；`source_frame_id`、机体 AGL 和航向等元数据在 C++ 侧按可空语义、
  有限数和范围严格校验，失败仍清空已加载状态。
- Fly View 回放弹窗增加“全部/可定位/有效测距/有媒体”四种只读筛选；每个事件可一键“复制证据”，复制文本包含
  事件/目标/捕获时间、源帧、车辆姿态、GPS、测距和完整媒体身份，仍不落图、不选目标、不创建任务、不发送飞控。
- C++ self-test、Python 创建的真实 bundle 由 native `--load` 跨语言复核、portable offscreen simple boot 均通过。
  v172 定向回归 45 tests：`45 passed, 0 skipped, 0 failed`；JUnit `artifacts/evaluation/target-event-replay-v172-focused.xml`，
  6,772 bytes，SHA-256 `FFC03FE6EB989B4119B8209A0753BAA6C87D78F20587D4B6B989591483A328AD`。
- 文档/parity/style 定向回归另有 72 tests：`72 passed, 0 skipped, 0 failed`；Ruff、format、compileall 与
  `git diff --check` 通过。
- 全量回归 3,708 tests：`3704 passed, 4 skipped, 0 failed, 0 errors`，耗时 194.025 秒；JUnit
  `artifacts/evaluation/target-event-replay-v172-full-rerun.xml`，542,180 bytes，SHA-256
  `31D7D474F947B1E4F648CCBC4C62404FCE10C44DEADDE83E16C3B1E156C78AF0`。
- Release/portable EXE 已重链并部署到
  `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`：
  46,869,504 bytes，SHA-256 `A1C52D9130386D8FAE4316EE23FD00D68E2141E6A385A7844229CB8680A79412`；
  native replay self-test 为 202,240 bytes、SHA-256 `7BD2F93FBA5889D548BB65F5F5E81AA780FE47747593439BA8B4805FDBA0F837`。
- `configs/release-parity.json` 当前为 10,129 bytes，SHA-256
  `A12A5490E73ED52266A44F447DFD87895CD837476895AFFF2EFD43CAAE09C827`。v171 派生证据与 E7 report-only 当时曾待
  重绑，已由 v173 在当前 parity 下重新生成；E7 仍 `gate_passed=false`、`release_allowed=false`，仓库基线 31 blockers 不变。
  现场网卡仍为 `192.168.1.13/24`，目标网段 `192.168.144.0/24`，Jetson/GR01/相机未连通；本轮没有提升室外 GNSS、
  真实硬件、HIL/REAL、人工 Fly View 或实飞状态。

## v171 本轮已收口（原生目标事件回放只读查看器）

- 将已有 `multidetect.target-event-replay.v1` 证据包接入原生 QGC Fly View：选择 `manifest.json` 后由
  `MultiDetectTargetEventReplayStore` 在 C++ 侧一次性校验 manifest、`events.jsonl` 与 `media/` 闭合目录，
  再以只读 `QVariantList` 投影到 QML 时间线；校验失败会清空状态，不保留半加载数据。
- 原生校验器拒绝重复 JSON key、未知字段、非有限数、路径逃逸/符号链接、时间/序列不连续、GPS 质量或新鲜度
  不合格、测距区间不合法、媒体额外文件和 bytes/SHA-256 不匹配；事件与媒体路径均限制在 manifest 所属目录。
- Fly View 目标卡片新增“载入回放/查看回放”入口和只读弹窗，显示 bundle/session、事件数量、目标类别、捕获时间、
  GPS、距离/方位、视频/深度引用及校验状态；弹窗没有落图、选目标、任务、飞控或物理释放调用。
- `MultiDetectTargetEventReplayStoreSelfTest` 通过，覆盖有效包、篡改媒体和重复 key 的 fail-closed 路径；Python
  `create_target_event_replay.py` 生成的真实 bundle 由 C++ self-test `--load` 跨语言加载通过。
- v171 定向回归 45 tests：`45 passed, 0 skipped, 0 failed`；JUnit `artifacts/evaluation/target-event-replay-v171-focused.xml`，
  6,772 bytes，SHA-256 `EAC8F4C1E543E517E90314060CA65A998AD00826DBC67A68598087C2518E9520`。Ruff、format、compileall 与
  `git diff --check` 通过。
- 全量回归 3,708 tests：`3704 passed, 4 skipped, 0 failed, 0 errors`，耗时 184.441 秒；JUnit
  `artifacts/evaluation/target-event-replay-v171-full.xml`，542,180 bytes，SHA-256
  `8074DBAD5781888E518F7A0826F30B9CDD34F4F654F0B2CE698754DA89CE2DC6`。
- Release portable EXE 已重链并部署到
  `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`：
  46,863,360 bytes，SHA-256 `589E33ADAB2C7D1DBC58524BCAE3474D18743A6E0D970FC4C01CF3F187201717`。
  `MultiDetectTargetEventReplayStoreSelfTest.exe` 退出 0；portable `--simple-boot-test` 退出 0，stdout/stderr 均为
  0 bytes，QML 初始化错误数 0。
- `configs/release-parity.json` 已重新生成：10,129 bytes，SHA-256 `D99F244ACFA708D16CC40019CA0ED8E8FCFCE827BFD250983A5FAC1A716ED23C`；
  UI `CC9841A00BF6B92F05C68C6CB26921EC8163F829A223B32B9C42131C55E5AADF`，core/task/safety/PX4 四组 hash 保持当前绑定。
- 派生证据也从原始 source 按 v171 parity 重算：external join/revalidation 为 5,492/565 bytes（SHA-256
  `9EE018D6B483511922FCE578E1B3E6E556B19C9488AC2A0B2E6C88FB774012FF` / `8D72ED39C260275CAA6E616FB7825A865CDE4ED3EF5E4062F0294EBBB671708A`），
  PX4 response matrix/campaign 为 6,985/26,120 bytes（SHA-256
  `1D67697C85CDEF0A1F4F3F37AE331EECCD62D0922323D8D87DD08D42A484ADF6` / `6766516C2D6A8E99A00947D8F76922D143A751FD0E49857E06BC5D0F669AA545`）。
- E7 report-only 已在新输出目录 `artifacts/evaluation/terminal-continuation-v171/release-gate/` 重新生成；manifest/report
  为 23,729/73,786 bytes（SHA-256 `B1CBCD26061925787C34FA02405818833CE9A1D80D4609F3967505558B62745E` /
  `5E7EC0DF188AB1E093B196B4E936E296EF0DCBDD73BDB8C1EE11A892780986E0`），当前仍 `gate_passed=false`、
  `release_allowed=false`，现场 freshness 超时与尚未完成的真实/人工门保持显式阻断。
- E7 仍为 `gate_passed=false`、`release_allowed=false`；仓库基线保持 `31 blockers`，本次 report-only 因现场报告
  新增 freshness 超时而列出 32 条详细原因。现场网卡仍是 `192.168.1.13/24`，目标网段
  `192.168.144.0/24`；Jetson `192.168.144.10:22`、GR01 `192.168.144.25:5760`、相机 `192.168.144.108:554` 仍未连通。
  本轮没有提升室外 GNSS、真实硬件、HIL/REAL、人工 Fly View 或实飞状态；当前查看器只读证据回放，不产生飞控指令。

## v170 本轮已收口

- Fly View 目标定位卡片现在直接读取 `MultiDetectState.targetObservationRecordingQualitySummary.current_status`，
  与诊断弹窗共用同一原生 recorder 摘要；卡片显示当前/上次录制状态、定位率、当前连续不可定位、最近采样断档和
  `target_not_locatable` / `range_invalid` / `gnss_unqualified` / `latest_sampling_gap` 原因。
- 没有录制样本时状态为中性 `WAITING`；录制停止后仍显示“上次定位质量”，不把历史摘要冒充当前采样。质量行只读，
  不改变 GPS/测距 fail-closed、目标坐标与 `1σ` 圆、不进入 PX4 Mission、飞控或物理释放路径。
- QGC custom 静态回归覆盖属性、四级状态映射、告警原因映射和主卡片绑定；Release/portable EXE 已重链并完成依赖部署，
  46,751,744 bytes、SHA-256 `9675AC36032CC990F1F27ACD638F1B4D1D0397F34D9B43810A95D0BCD0D2D263`。
- 定向回归 89 tests：`88 passed, 1 skipped, 0 failed`，耗时 2.194 秒；JUnit 14,124 bytes、SHA-256
  `C56C01C84072E0EC1B51F09FA6C9FCE90E37A85F530FCF034BFB0779E91F79B9`；Ruff、format、compileall 与 diff check 通过。
- 全量回归 3,707 tests：`3703 passed, 4 skipped, 0 failed, 0 errors`，耗时 183.031 秒；JUnit 542,030 bytes、
  SHA-256 `3B21593CB2277F1762D70C2287B34309EC615EE3456BC6D9DECDE9736AC7B3E1`。
- portable offscreen simple boot 退出 0，stdout 0 bytes，stderr 879 bytes、SHA-256
  `EBB0811E76009EAA70CEC8B19F52B3C2B3232340FAE78CDD7CC67A505644D7F9`，QML 初始化错误数 0。
- v170 重新生成并独立验证 external join/revalidation、PX4 三机型 response matrix、6 正例/9 故障 campaign、
  现场只读 preflight/interface diagnostic 和 E7 report-only；现场接口仍为 `192.168.1.13/24`，目标网段为
  `192.168.144.0/24`，Jetson/GR01/相机均超时。E7 仍为 `gate_passed=false`、`release_allowed=false`、
  `31 blockers`，本轮不提升室外 GNSS、硬件、HIL/REAL、人工视觉或实飞状态。
- 当前派生证据均留在 `artifacts/evaluation/terminal-continuation-v170/`：external join/revalidation 为
  `5,491/565` bytes（SHA-256 `7380455757770BC76C22FBF1C53FBC18B1AE37A97022E431B0F801A491BDCDDD` /
  `BABB9B571A399CAC270938FB4923AB7C5480F6A9DBBCA36A701FD415807B6824`）；PX4 matrix/campaign 为
  `6,985/26,120` bytes（文件 SHA-256 `8D26F7633D5DD353FC9CDC93413334432458F7087F43E278013AA9FF9D90C7A1` /
  `32D5B33F38290CE6B99EFA3B37A1A9C5D7723B197D96A44C4CC9FCB599E2CBBC`）；现场 preflight/interface 为
  `4,668/4,403` bytes（SHA-256 `83C33525C1DF94E696FA876837E4F07F125F0023F1A99749C794574431043574` /
  `AAED1FCF57471A76B745CAFD1192A4D089F781D800A5764C4F72AB53605E09C4`）。

## v169 本轮已收口

- 原生录制器的实时质量摘要新增 `current_status`：按当前连续状态给出 `WAITING`、`HEALTHY`、
  `WARNING`、`CRITICAL` 四级结果，并暴露当前连续不可定位/测距异常/GNSS 不合格计数、最近采样间隔、
  断档告警原因和严重级别阈值；历史质量分段与停止后的 manifest 绑定离线 verifier 保持不变。
- Fly View 诊断弹窗新增当前状态颜色、中文状态文本和实时计数；质量状态仍是只读 advisory，任何质量告警
  都不会生成坐标、任务或飞控输出。
- C++ self-test 覆盖 `WARNING → CRITICAL → HEALTHY` 恢复路径；安装式 verifier 独立复核 4 样本会话，
  2/4 可定位、定位率 `0.5`、最长连续不可定位 `2`、最大断档 `2,000 ms`，报告 7,795 bytes、SHA-256
  `580898F32543B8EBAEE78E0099DDD46AACAC878066DA221AB3D431B1325C4BD4`。
- Release/portable EXE 已按 v169 重链并完成依赖部署：46,751,232 bytes、SHA-256
  `5BC3761AE4C13C28EBE6BB34691F6EB350CA9EDEF96376DF0DDD03DBE73C5A3A`；portable offscreen simple boot
  退出 0，stderr 879 bytes、SHA-256 `D2BE8F83902FF9AF48F105685EF17B4571075F826E6A0126603DCBC670D41F81`。
- 定向回归 86 tests：`85 passed, 1 skipped, 0 failed`，耗时 2.412 秒；JUnit 13,696 bytes、SHA-256
  `8721CA481C38579F34E4916C05D1F496461B1A037999B6D0F267C924DD807135`。最终全量回归 3,707 tests：
  `3703 passed, 4 skipped, 0 failed, 0 errors`，耗时 182.008 秒；JUnit 542,030 bytes、SHA-256
  `B0010AE56D61353559AF5C200E28D0A7310DF7650962FBDEA35406D18487B8D1`。
- parity 9,956 bytes、SHA-256 `FB645D12D0CF0DD4CCEFF68CFC0D15D1E4BCFC25FD574301C4790D61D3F94A05`；
  UI contract 为 `6C6BECB3C9890EAF3360FFBECADC2371923B0DE5014723CB8DC911DDE47CCC15`，其余四组不变。
- 三机型 external join/revalidation、PX4 reported-position matrix、6 正例/9 故障 campaign、只读现场链路
  和 E7 已按 v169 parity 重算并绑定。现场接口仍为 `192.168.1.13/24`，不在目标 `192.168.144.0/24`，
  Jetson `192.168.144.10:22`、GR01 `192.168.144.25:5760`、相机 `192.168.144.108:554` 均超时；E7
  仍为 `gate_passed=false`、`release_allowed=false`、`31 blockers`。本轮不提升室外 GNSS、硬件、HIL/REAL、
  人工视觉或实飞状态。

## v168 本轮已收口

- 原生录制器现在在每次成功保存快照后同步维护只读实时质量摘要：样本/可定位数量、定位率、最长连续
  不可定位、观测时长、采样间隔/断档、三类质量分段和 producer epoch 切换都可直接供 QML 使用。
- Fly View 诊断弹窗新增“录制质量 · 实时只读”区域与质量时间线；红/黄/橙分别表示不可定位、测距
  无效和 GNSS 不合格，详细列表保留分段范围与 epoch 切换数量。停止录制后的正式结论仍由 manifest 绑定
  的独立离线 verifier 生成，实时 UI 不自封为最终证据。
- C++ self-test 生成 4 样本会话，覆盖 2 个连续不可定位样本、2,000 ms 断档、三类各 1 个质量段与
  1 次 epoch 切换；安装式 verifier 独立复核后 `gate_passed=true`。报告 7,780 bytes、SHA-256
  `EB06060134414907BBDAB327553958A28FEB7AF2E7FCB22D9B96D9D2C65221BB`。
- Release/portable EXE 已重链并完成依赖部署：46,746,112 bytes、SHA-256
  `F74F3D6FCFC8C044745A2B75A8ED07FD4CEBD41BDE2BD1239E00F9FB40C9A9FA`；offscreen simple boot 退出 0。
- 定向回归 86 tests：`85 passed, 1 skipped, 0 failed`。parity 9,956 bytes、SHA-256
  `DE1686C5844B11D30D2AA532FE701FA55E9E909C221CA621650FCBA6C1E5F997`；UI contract 为
  `47A6E679A127FCF7E80A3723D85A8756BD50CA87CD4B546B5A4D2D537855DB3`，其余四组不变。
- 最终全量回归 3,707 tests：`3703 passed, 4 skipped, 0 failed, 0 errors`，耗时 182.514 秒；JUnit
  542,030 bytes、SHA-256 `B15A4B123DBF024311C3DB5E3A74B8F19A6C08115839AA09FD44090B71B23B04`。
- SIM/PX4 派生证据、现场只读探测和 E7 已按 v168 重算。现场仍是 `192.168.1.13/24` 错路由且三端点
  超时；E7 保持 `false / false / 31 blockers`。功能继续为 advisory-only，不提升室外 GNSS、硬件、
  HIL/REAL、人工视觉或实飞状态。

## v167 本轮已收口

- manifest 绑定 session 报告新增观测总时长、采样间隔最小/最大/平均值、manifest 期望间隔、容差、断档
  事件和违规数量；无需人工翻查数千份快照即可发现采集停顿或丢帧区间。
- 新增连续质量分段：`non_locatable`、`range_invalid`、`gnss_unqualified` 分别记录开始/结束样本、UTC、
  本地时间、持续时间、样本数、诊断原因、测距状态和 GNSS 原因；epoch 切换也记录前后 epoch 与发生点。
- 新增 `--maximum-non-locatable-run` 与 `--maximum-sampling-gap-ms` 两个可选门，超过允许的连续不可定位
  样本数或最大采样间隔时会保持报告可审计但 `gate_passed=false`。
- 安装式 CLI 对 v165 C++ 双快照目录以定位率 1.0、最大不可定位连续数 0、最大间隔 1,000 ms、零质量
  异常段通过严格门；报告 3,349 bytes、SHA-256
  `BF033208CA17673E7F1D6F6ECB6701FDEB89A67555B772748473423CE3617471`。
- 定向回归 90 tests：`88 passed, 2 skipped, 0 failed`。parity 9,956 bytes、SHA-256
  `9F0583CAD05B62E14D82C832209FB2D1EBEEDDC6A61E7BA90DAA928C114EC817`；UI contract 为
  `38269F81C6FAFE0E2A47ADD5FE874336BA7E9E1EB6C6F2CA25C0FE35A655138D`，其余四组不变。
- 最终全量回归 3,707 tests：`3703 passed, 4 skipped, 0 failed, 0 errors`，耗时 188.534 秒；JUnit
  542,056 bytes、SHA-256 `2C91D31119AD87B16987A59675C179E0AB1922E19091FA70193B47DDB01B6178`。
- SIM/PX4 派生证据、只读现场链路和 E7 已重算；现场仍从 `192.168.1.13/24` 错路由且三端点超时，
  E7 保持 `false / false / 31 blockers`。本轮不重链 EXE，不提升真实硬件、室外 GNSS 或实飞状态。

## v166 本轮已收口

- 为原生录制 manifest 增加闭合 Draft 2020-12 schema、严格 Python verifier、仓库 wrapper 和安装式
  `multidetect-target-observation-recording-manifest-verify`。验证拒绝重复/未知字段、非有限数、符号链接、
  路径逃逸、重复引用、bytes/SHA-256 不符、时间区间/顺序错误及未被 manifest 引用的额外快照。
- session verifier 自动识别单目录中的 manifest；`--require-recording-manifest` 强制目录必须完成 manifest
  校验，并把 manifest 路径、bytes、SHA-256、session ID、停止原因、样本数和目录闭合状态写入会话报告。
  零样本 manifest 可以作为结构有效的短会话记录，但不能通过需要实际诊断样本的 session 门。
- 安装式 CLI 对 v165 C++ 实际会话验证通过：manifest 报告 2,288 bytes、SHA-256
  `A6C4F145B4C8DDBED6D3049667588A34EC601D8372D731CDB8767EA59E4D15AC`；manifest 绑定会话报告
  2,832 bytes、SHA-256 `93F6A85D1D662B4744FF17A6B3F1750CECCE022766849538A52C35B9789133EB`。
- 定向回归 84 tests：`82 passed, 2 skipped, 0 failed`；两个 skip 仍是 Windows 当前权限下的符号链接测试。
- parity 为 9,956 bytes、SHA-256
  `0D74691C5655ACB1AE49EB99AA29B303A21C29FD3ECC23007696DB719DC935E3`；UI contract 更新为
  `F472E0188120B71A9B8FA33588BA91957EDDF9BF6FC4F1157F09CA7FBBE279B5`，其余四组不变。
- 派生 SIM/PX4、只读现场链路和 E7 已按 v166 parity 重算；现场仍从 `192.168.1.13/24` 错路由且三端点
  超时，E7 保持 `false / false / 31 blockers`。本轮仅增强离线证据完整性，不重链主 EXE，也不提升
  人工、室外 GNSS、硬件、HIL/REAL 或实飞状态。
- 最终全量回归 3,701 tests：`3697 passed, 4 skipped, 0 failed, 0 errors`，耗时 269.920 秒；JUnit
  541,038 bytes、SHA-256 `2B9EFE6FC0E4579AB487E6D27485B0B56189E29C083E5E9A818E36DF01964AAA`。

## v165 本轮已收口

- 原生 QGC 新增目标定位诊断连续录制：Fly View 可一次开始/停止，以 `1 Hz` 保存严格 v163 快照，单次
  最多 3,600 份；UI 显示活动状态、样本数与会话目录，应用退出、采集失败和上限到达都会执行明确收口。
- 每次录制使用唯一 `target-observation-session-*` 目录；快照沿用原子保存，正常停止以原子替换写入
  `multidetect.target-observation-recording-manifest.v1`，记录停止原因及每份相对路径、bytes、SHA-256。
  异常退出时已完成快照仍保留，不能伪造一个“已完成” manifest。
- C++ recorder self-test 真实生成 2 份快照和 manifest；安装后的
  `multidetect-target-observation-session-verify` 独立复核得到 2/2 可定位、定位率 1.0、门通过。会话报告
  2,272 bytes、SHA-256 `4E463C50144C83E40A50B472F97539D9C7B522683F81CEAB1AF2C146F6F344F5`。
- Release/portable EXE 已重链：46,718,976 bytes、SHA-256
  `8F600A9CE8D1C4E8AF1A63721F16E52A67E56715A81B2EE7D469AED62C1BAB6D`；portable offscreen simple
  boot 退出 0，无 QML 初始化失败。
- parity 为 9,750 bytes、SHA-256
  `149E83944D7E90331A9940665C14D5EAB9387533C49F2308C1027A7903A11496`；UI contract 更新为
  `1B14A62D40FDA9C76EC37C9E789A25D2123DB4DFF489CB61CF263888CB1B7EA1`，其余四组未变。
- 派生 SIM/PX4 证据与 E7 已按 v165 parity 重算。只读现场探测确认系统仍从 `192.168.1.13/24` 路由，
  Jetson/GR01/RTSP 三端点超时；E7 保持 `gate_passed=false / release_allowed=false / 31 blockers`。
  录制功能固定 advisory-only、飞控关闭、物理释放关闭，不提升人工、硬件、室外 GNSS、HIL/REAL 或实飞状态。
- 最终全量回归 3,693 tests：`3689 passed, 4 skipped, 0 failed, 0 errors`，耗时 196.262 秒；JUnit
  539,793 bytes、SHA-256 `16793FE3E14EFB14F9D3CFA8404EB048678A6D5507E40D666ABB4CE1BD1E264C`。

## v164 本轮已收口

- 新增闭合 `multidetect.target-observation-diagnostics-session.v1` 会话报告、严格 verifier、仓库 wrapper
  和安装式 `multidetect-target-observation-session-verify`。输入可为显式快照或诊断目录；目录只读取标准
  命名 JSON，拒绝空集合、重复解析路径和符号链接。
- 每份文件先完整经过 v163 单快照 verifier，再按 `local_now_ms` 确定性排序。会话拒绝时间重复/回退、
  同 producer epoch 的 sequence/`sentAtMs` 回退以及 retired epoch 重新出现；不同 epoch 的远端时钟不做
  错误的跨时钟比较。
- 报告逐样本绑定绝对路径、bytes、SHA-256、生成时间、定位状态、失败原因与原子身份，并汇总定位率、
  最长连续不可定位区间、epoch 切换、失败原因、range validity 和 GNSS reason。`--minimum-samples` 与
  `--minimum-locatable-rate` 不满足时 `gate_passed=false`。
- 安装后 CLI 消费实际 C++ v163 输出形成 1 样本跨语言会话报告：1,371 bytes、SHA-256
  `787AA4D96CFE89DE03E480E2EC988F59A83F56D750F6AF688E3B81497B98E781`，定位率 1.0、门通过；
  连续性正/负路径由多样本测试覆盖。
- 定向回归 `76 passed, 2 skipped`；JUnit 12,991 bytes、SHA-256
  `D8334CC318D597183C107884622839B7866BC516DD21170B70AC65885141EBE5`。两个 skip 都是 Windows
  当前权限下的符号链接创建测试。
- parity 为 9,571 bytes、SHA-256
  `FCEE7CDC23D14E49017CC5EAF8D046021B785F4526BF6D704D458BACA94F0DEB`；UI contract 为
  `DB582F452060E8AD33717DA4B4147BBB05168555AB2D182EE61A3327D2D08127`，其余四组未变。
- 派生证据和 E7 已按 v164 parity 重算。现场仍是 `192.168.1.13/24` 且三端点超时，E7 保持
  `gate_passed=false / release_allowed=false / 31 blockers`。本轮未重链 EXE，也未提升人工、硬件、
  室外 GNSS、HIL/REAL 或实飞状态。
- 最终全量回归 3,693 tests：`3689 passed, 4 skipped, 0 failed, 0 errors`，耗时 208.55 秒；JUnit
  539,819 bytes、SHA-256 `B325B2157890E27BCD71EF2DEAF29DE61DB769918E9C72ECDE9C8AA0B3AA83C8`。

## v163 本轮已收口

- 新增闭合的 `target-observation-diagnostics.schema.json` 与严格离线 verifier；仓库 wrapper 和安装后的
  `multidetect-target-observation-diagnostics-verify` 使用同一实现。输入限制为普通非符号链接文件、最大
  1 MiB，并拒绝重复 JSON key、`NaN/Infinity`、指数溢出得到的非有限数和未知字段。
- verifier 不信任 producer 的派生结论：会重算三路 `producerEpoch/sequence/sentAtMs` 原子身份、源年龄、
  本地接收年龄、`status_fresh`、坐标省略和定位条件。结构有效的 fail-closed/启动期快照仍可留档；使用
  `--require-locatable` 时不可定位快照返回非零，且任何失败都保持飞控和物理释放为 false。
- C++ 原生 self-test 新增 `--emit`，从实际诊断实现原子写出 JSON。该文件为 2,164 bytes、SHA-256
  `10CD3085B24130FA32965A063378EB49B3BFDFF02230B88F86FABB5E76A15E39`，仓库 verifier 与安装后 CLI
  均通过 `--require-locatable`；验证报告 746 bytes、SHA-256
  `F29C25BD3EFE969E17701ABE39D40773C42C1BF0E3B9B1D8B95FE4800E28BB61`。
- 定向回归为 `63 passed, 1 skipped`；skip 仅为 Windows 当前权限不允许创建测试符号链接。JUnit 9,974
  bytes、SHA-256 `7B64B69FE49432E3BF6036F965AC6F0C645E10CD64574A31F2D88E2D226AA330`；Ruff、
  compileall 与 diff check 通过。
- 当前 parity 为 9,362 bytes、SHA-256
  `C5E7ACF50EE3EF1E7B7FF377F04FA767B3240CBB21A0B16E65416BC94BBD9ECC`；UI contract 为
  `A437B8855BD6570F31C21C66AB85CF25661318CEAE62A61B58720550EF66A75A`，其余四组 hash 未变。
- external join/revalidation、response matrix、6 正例/9 故障 campaign、只读现场探测和 E7 已按 v163
  parity 重算。现场仍为 `192.168.1.13/24`，Jetson/GR01/RTSP 超时，`hardware_link_ready=false`；
  E7 保持 `gate_passed=false / release_allowed=false / 31 blockers`。
- 本轮没有修改或重链主 QGC，portable EXE 继续使用 v162 已验证的 46,704,640 bytes、SHA-256
  `ECB14FA929AB117EC42178A305D531B8E03FAF1EB283260E551F67E0DF3E28B4`。人工视觉、室外 GNSS、
  HIL/REAL、真实硬件与实飞状态没有升级。
- 最终全量回归 3,679 tests：`3676 passed, 3 skipped, 0 failed, 0 errors`，耗时 194.79 秒；JUnit
  536,802 bytes、SHA-256 `F9FCCF6AF34849B7BF8D24F3E1F628C1C06771241DAEF2608309C0693629B990`。

## v162 本轮已收口

- Fly View 新增“目标定位诊断 · 只读”原生弹窗；打开时冻结当前诊断 JSON，避免阅读期间由实时状态变化
  拼出混合快照。机器 reason code 映射为中文现场原因，并显示原子身份、目标/源帧、测距与 GNSS
  质量、源年龄和本地接收年龄。
- 坐标显示继续由诊断文档的 `coordinates_included` 单向控制；失败状态明确显示“不可定位 · 坐标已强制
  省略”，不会从 QML 状态旁路恢复经纬度。弹窗可复制或原子保存同一 schema JSON，保持 advisory-only。
- Release/portable EXE 字节一致：46,704,640 bytes，SHA-256
  `ECB14FA929AB117EC42178A305D531B8E03FAF1EB283260E551F67E0DF3E28B4`；offscreen simple boot
  退出 0，stdout/stderr 均为 0 bytes，无 QML 初始化错误。
- v162 parity 为 9,177 bytes、SHA-256
  `4EEFE598F7BEDCA631A0583DC1AB5AB0494A9C2BB89FCD98043E3C76A52E6D6F`；UI contract 更新为
  `C230725F927941B102AC3FEE6A24D7A2A6D1E1A00117A8B5B86ECA4AB4AE8829`，其余四组 hash 未变。
- 外部三机型 join/revalidation、response matrix、6 正例/9 故障 campaign 与 E7 已按 v162 parity 重算。
  只读现场探测仍为 `192.168.1.13/24`，Jetson/GR01/RTSP 超时，`hardware_link_ready=false`；E7
  正确保留 `false / false / 31 blockers`，人工视觉、室外 GNSS、HIL/REAL、真实硬件和实飞均未通过。
- 最终全量回归 3,662 tests：`3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 272.67 秒；JUnit
  533,660 bytes、SHA-256 `0D16ED23B6849934FAF176BD6039BFAACAE94B0D11A3066EA1968F1015C41C56`。

## v161 本轮已收口

- 新增 `MultiDetectTargetObservationDiagnostics`：把 range、target geolocation、GNSS、producer epoch
  摘要、三路原子 envelope 身份、源年龄和本地接收年龄汇总成
  `multidetect.target-observation-diagnostics.v1`；Fly View 卡片可一键复制或原子保存。
- 定位诊断严格复核三路同 `producerEpoch/sequence/sentAtMs`、同目标/源帧、合格 GNSS、合格测距、
  有效坐标/1σ 以及 1500 ms 源/接收新鲜度。失败快照仍保留结构化原因供现场排错，但强制
  `locatable=false`、`coordinates_included=false`，并删除经纬度与 1σ，不能形成假落点。
- 新原生 self-test 覆盖有效原子三元组、GNSS 不合格、sequence 错配、接收过期、失败快照坐标删除和
  原子文件保存。连同 operator protocol、target evidence、producer epoch、44-fragment depth-grid 和
  RTSP wire，共六项原生 self-test 通过；focused QGC/parity 45 项、Ruff lint/format 通过。
- Release/portable 主 EXE 为 46,702,592 bytes，SHA-256
  `729BEDA95DD42E70538D854181113BD09D2414EED4D787AAA11529D1F55C05A0`；portable offscreen simple
  boot 退出 0，stdout 0 bytes，stderr 220 bytes、SHA-256
  `B7E0728E90A02F3FE1FF8FC9C1CB46B53BA296050981F4F8975A32C27FCA03A3`，无 QML 初始化错误。
- `release-parity.json` 为 9,177 bytes、SHA-256
  `72840CB9F1D3FE85A3509087B5331F4B11EDDBF131CB772919CC38CAC94471BC`；UI contract 更新为
  `3BC3C5B2E983A0D25F385FFAAEDEA163C83640D750684DEAA0F3853C7D282DB1`，core/task/safety/PX4 未变。
- 派生 SIM/PX4 证据与 E7 已按最终 v161 parity 重算。最新只读现场探测仍为
  `192.168.1.13/24`，不在预期 `192.168.144.0/24`；Jetson/GR01/RTSP 三路超时，
  `hardware_link_ready=false`。E7 正确保留 `false / false / 31 blockers`；人工 Fly View、室外 GNSS、
  HIL/REAL、真实硬件和实飞均未通过。
- 最终全量回归 3,662 tests：`3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 273.29 秒；JUnit
  533,660 bytes、SHA-256 `8443ED203DC647B65EF0D7C4723FD5022BB791B02E746056D021635A756F929C`。

## v160 本轮已收口

- 新增原生“生产者 epoch 诊断”弹窗，以最新事件优先的方式展示最多 64 条生命周期/事故事件，并明确
  显示健康摘要、total/retained/dropped、当前 epoch、三类累计事故计数和最近切换/事故时间。
- 事件 journal、诊断 JSON 和文件保存抽为 `MultiDetectProducerEpochDiagnostics`；卡片和弹窗均可一键
  复制 JSON 或保存 JSON。保存使用同目录临时文件 + flush/close + rename 的原子提交，成功文件位于
  `AppLocalDataLocation/diagnostics`；失败原因进入现有 operator error 状态。
- 新原生 self-test 覆盖零事件 `WAITING`、64 项上限与 dropped 计数、newest-first、JSON round-trip 和
  原子保存。连同 operator protocol、target evidence、44-fragment depth-grid 和 RTSP wire，共五项原生
  self-test 通过；focused QGC/parity 回归 45 项与 Ruff 通过。
- Release/portable 主 EXE 为 46,665,216 bytes，SHA-256
  `DE323538B410D6264825B071A083620471A4D726921FD25F10CB859476DB1BAE`；portable hidden/offscreen
  simple boot 退出 0，stdout 0 bytes，stderr 220 bytes、SHA-256
  `19AD74FE74B1C4A09CB408506D7523CFF73B9B1134157A8180820CBF8AC4054F`，无 QML 初始化错误。
- `release-parity.json` 为 8,992 bytes、SHA-256
  `B5CCB1AA12CEB4D661F940D5FE814A5A6F21EEC9DFD861C1D8DE4EC56F139E2C`；UI contract 更新为
  `02033DDF7414F042DCC47AF0B02E175C9C620BF3C4FC1F679028F2CFD7C5093A`，core/task/safety/PX4 identity 未变。
- external join/revalidation、三机型 response matrix、6 正例/9 故障 campaign 与 E7 已按最终 v160 parity
  重算并复验。只读现场探测仍从 `192.168.1.13` 错路由，Jetson/GR01/RTSP 3/3 超时；E7 仍为
  `gate_passed=false`、`release_allowed=false`、31 blockers。人工 Fly View、室外 GNSS、真实硬件、
  HIL/REAL 与实飞仍未通过。
- 修正前的第一次全量运行暴露 3 个暂态失败：进度 JSON 与 terrain truth 仍绑定旧 parity；重新生成 parity、
  重建 portable、重绑派生证据并更新文档后，最终全量回归 3,662 tests 为
  `3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 292.93 秒。JUnit 533,634 bytes、SHA-256
  `F63533BB049166A87F9109C8B6E6B19C30673C4069210D5285D7864755776DA6`。

## v159 本轮已收口

- QGC controller 新增 producer epoch 生命周期/事故时间线：初始化、生产者重启、退休旧包拒绝和切换容量
  耗尽均记录事件索引、发生时间、incoming/previous/active epoch、健康级别/原因与累计事故计数。
- 时间线最多保留 64 条，并分开记录 total/retained/dropped；诊断环形淘汰不影响 32 项 retired epoch 的
  绝不淘汰安全状态机。“目标落图”卡片可一键复制 `multidetect.producer-epoch-diagnostics.v1`，零事件
  `WAITING` 状态也可以导出；JSON 固定声明 advisory-only、飞控关闭和物理释放关闭。
- focused QGC/parity 回归 45 项、Ruff lint/format、三项关键 native self-test 和 portable offscreen
  simple boot 均通过。Release/portable 主 EXE 为 46,659,072 bytes，SHA-256
  `7DA7F882ECB51EEA4DDF1D79FF5F9EBB52E8BD3B77F0DBEEAC1B23EFFB95E375`；功能提交为
  `1e95d6eadcb06dadb5970682dcd442a0a2c9c03f`。
- `release-parity.json` 为 8,815 bytes、SHA-256
  `2FB2AE51AD9E5D5011D51C9F27379936F811C3A371EA76D82334B735B8024CAE`；UI contract 更新为
  `049B1A6B1C26F80D2627DA7DB9DE2A8D733EDE1B3E4DAC972F27D411065505F4`，core/task/safety/PX4 identity 未变。
- v159 external join/revalidation、三机型 response matrix、6 正例/9 故障 campaign、只读 field-link 与 E7
  已按当前 parity 重算。控制机仍从 `192.168.1.13/24` 错路由，Jetson/GR01/RTSP 3/3 超时；E7 保持
  `gate_passed=false`、`release_allowed=false`、31 blockers，未修改网络、飞控、载荷或受保护服务。
- 最终全量回归 3,662 tests：`3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 342.548 秒；JUnit
  533,635 bytes、SHA-256 `C96C2069787576D8A2118A8C16A1D691E54AC6259A14E76ABEB7A6DCF1B2A139`。
  人工 Fly View、室外 GNSS、真实硬件、HIL/REAL 与实飞仍未通过。

## v158 本轮已收口

- QGC controller 将 producer epoch 健康锁存为 `WAITING / HEALTHY / NOTICE / WARNING / CRITICAL`；
  `MultiDetectState.qml` 投影健康级别、原因和最近事故时间，“室外测距质量”以中文和颜色直接区分状态。
- `MultiDetectObservationEpoch` 的 retired 集合改为固定 32 项且不淘汰。容量耗尽时返回
  `CapacityExhausted`，不切换 active epoch；原生测试覆盖填满容量、第 33 次切换 fail-closed，以及最早
  retired epoch 仍不能复活。该保护要求 QGC 重启后才能接受更多 session 切换。
- 目标证据升级为 `multidetect.target-evidence.v6`，持久化健康级别/原因、三类计数、切换与事故时间；
  v1-v5 只读兼容，字段类型或级别/计数/时间不一致的 v6 记录 fail-closed。
- focused Python 回归 144 项通过；operator protocol、target evidence store、replay RTSP wire 三项关键
  native self-test 均退出 0。Windows Release/portable 主 EXE 均为 46,650,880 bytes，SHA-256
  `8EBAB7D38113418582AAC12A226BBDFAF988EF30D3E7FCC927222B58E2332A69`；功能提交为
  `3c04724d91ca40a7ce287d214a32995fbbedc8b2`。
- portable hidden/offscreen simple boot 退出 0；stdout/stderr 均为 0 bytes，无 QML 初始化错误。
- `release-parity.json` 为 8,815 bytes、SHA-256
  `DAD414BDEF9B25EBF770A0CC9FF1EF4068914916DF2D8FD496282629BB8F2D37`；UI contract 更新为
  `74F616ABFC39A52AAF189A5030F2215F955EF426765C219E3CB5EF28CE46AFD3`，core/task/safety/PX4 identity 未变。
- v158 external join/revalidation、三机型 response matrix、6 正例/9 故障 campaign 与 E7 已按当前 parity
  重算。只读现场探测仍从 `192.168.1.13/24` 路由，Jetson/GR01/RTSP 3/3 超时；E7 仍为
  `gate_passed=false`、`release_allowed=false`、31 blockers。未修改网络、飞控、载荷或受保护服务。
- 最终全量回归 3,662 tests：`3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 264.284 秒；JUnit
  533,660 bytes、SHA-256 `331FD13D6E928168856ACB7C2C0D31EC0CAF56F49A733AEFB4ABFAEE6A7AAAE9`。
  人工 Fly View、室外 GNSS、真实硬件、HIL/REAL 与实飞均未通过。

## v156 本轮已收口

- 类型 `26` 新增必填非零 `producer_epoch`，紧凑 body 扩展后整个 application payload 恰好 128 bytes，仍为
  单个 MAVLink `TUNNEL`。`LiveOperatorBridge` 每次启动默认生成新的随机非零 epoch，同一进程发布的 range、
  target geolocation 与 GNSS 原子三元组共享它。
- QGC 新增 `MultiDetectObservationEpoch` 状态机。新 epoch 到达时先原子清空旧三类可见状态、时间戳、source
  replay guards 与类型 10/22/25/26 sequence gates，再采纳新三元组；旧 epoch 被记为 retired，迟到包永久拒绝。
  原生回归覆盖 producer A、切换 producer B、迟到 A 被拒且 B 仍保持 active。
- QML 要求 range/geolocation/GNSS 同时具有相同的正 epoch、sequence 与 `sentAtMs` 才允许落图/保存；新证据为
  `multidetect.target-evidence.v5` 并保存 `producer_epoch`，v1/v2/v3/v4 只读兼容。变化仍是 display/advisory-only。
- focused Python/QGC static 回归 133 项通过，Ruff 通过；operator protocol 与 target evidence store 原生自测通过。
  Windows Release 与 portable 主 EXE 均为 46,639,616 bytes，SHA-256
  `8BDDA7ABB9DF806C5B18DA0CFDD4169CFF1A556DD062B3784F6A09D8074DACF5`；功能提交为
  `cc0e3f0412b64c6b4450f1496bb1a4f71654f30d`。portable hidden/offscreen simple boot 退出 0；stderr
  1,009 bytes、SHA-256 `A51779C621AEDD42B2D38FA63C07B6F4D7A77240784DCFAECF3AD1DBA71F9873`，无 QML 初始化错误。
- `release-parity.json` 为 8,815 bytes，SHA-256
  `CEB14C70D5FB37085534B62C703DD76C305F4A9C866BD855250932056FF44BB1`；UI/task contract 更新为
  `431E8279302C88283718C7722FCC71BAFC9500AF0A5730B290EFBDEF55F62D54` /
  `7F8721C1561F5CBEB7A3CC41A7CDE2FACE1B58F1EEE8B33976A5F35AEF8BCD5C`，core/safety/PX4 identity 未变。
- v155 派生文件在 parity 变化后保留为历史证据；v156 external join/revalidation、三机型 response matrix、
  6 正例/9 故障 campaign 与 E7 已按当前 parity 重算。最新 E7 仍为 `gate_passed=false`、
  `release_allowed=false`、31 blockers；人工 Fly View、室外 GNSS、真实硬件、HIL/REAL 与实飞均未通过。
- 最终全量回归 3,662 tests：`3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 310.24 秒；JUnit
  533,660 bytes、SHA-256 `79A05CF85681A6964998A77B04F7AEE0B6B0E193D18FFA5A595FF8286ADE9445`。

## v155 本轮已收口

- 新增认证 operator protocol 类型 `26` `TARGET_OBSERVATION_STATUS`，把 range、target geolocation 与
  `GNSS_STATUS` 压入一个 124-byte application payload，可装入单个 MAVLink `TUNNEL`。三者必须共享
  `produced_at_s`，range/geolocation 的 target、frame 与 capture identity 必须一致；可用坐标必须绑定
  qualified GNSS。缺失、错帧、身份或质量矛盾、编码/发送失败均整包零发布，指纹不推进并保留重试。
- QGC C++ decoder 在认证、重放、sequence、source freshness 之后验证类型 26 的全部 registry、范围、
  坐标、GNSS canonical quality 与 fail-closed 不变量；controller 只会一次性采纳三个嵌套 map，并给它们
  注入同一外层 sequence/`sentAtMs`。QML 保存门要求三者均标记 atomic 且 sequence/发送时刻完全相等。
- 本地目标证据升级为 `multidetect.target-evidence.v4`；新写入只接受类型 26 原子三元组，v1/v2/v3
  保持只读兼容并在历史卡片区分旧语义。该观测固定 display/advisory-only，不启用飞控或物理释放。
- selected 回归、Ruff、Ruff format、compileall 与 `git diff --check` 通过；operator protocol、target
  evidence store、44-fragment depth-grid Python→C++ HMAC roundtrip、replay RTSP wire 四项原生自测均退出 0。
  portable `--offline --simple-boot-test` 退出 0；日志 1,001 bytes、SHA-256
  `131508B6705306BD09D6F8889CEDE9B30DCDB37B1526BD742EA2CF1DFB3EC220`，无 QML 初始化错误。
- 最终全量回归 3,662 tests：`3660 passed, 2 skipped, 0 failed, 0 errors`，耗时 280.19 秒；JUnit
  533,634 bytes、SHA-256 `45E3B03A225840ED747520006B7B458F7F533A7AA0489941540C28FDB085ECF0`。
- Windows Release/portable EXE 为 46,633,984 bytes，SHA-256
  `788E82B4BF8198EFC8ECD720E2E1A883899D7ADD65DE1276A0221FD61D35AC8F`。功能提交为
  `dc801caca24ff56b7eeb22c330c465e955562e34`。
- `release-parity.json` 为 8,735 bytes，SHA-256
  `289FD7BD12E12B3931D5AEDD3B5C94A34EF5559480F338B6AA139C9D07184CFF`；UI/task contract 为
  `3BA8008A73ED19BD9C5B774A322989A841263B947A395C977A660C55176BEBD7` /
  `2CDB5814C09DFAD6609B60C71AE9AFFE1AD5FFCEE63F5E4964590799F34C254B`，其余三组身份未变。
- 当前 parity 的 external join/revalidation、三机型 response matrix 与 6 正例/9 故障 campaign 已重算通过。
  新鲜只读 field-link 仍为 `192.168.1.13/24` 错路由且 Jetson/GR01/RTSP 3/3 超时；E7 manifest/report
  SHA-256 分别为 `97CE95947B99F2FC0D9633B53A9347ECEB237EE1467A9DB052A2E7A7FDAD6767` /
  `996F595DE8E07EB41F63A1EDB887637DC8ABB0B5ACD6A5197D597C549E93CCEF`，结果保持
  `gate_passed=false`、`release_allowed=false`、31 blockers。没有修改网络、受保护服务、飞控或载荷；
  室外 GNSS、人工视觉、真实硬件、HIL/REAL 与实飞均未声明通过。

## v154 本轮已收口

- Jetson operator bridge 在目标定位到期时把 `GNSS_STATUS`、range 与 target geolocation 作为同一生产帧组
  校验：三者必须存在且 `produced_at_s` 精确相同，可用目标定位还必须配合 qualified GNSS；缺失、错帧或
  质量矛盾均 fail-closed。发送顺序固定为 GNSS → range → target geolocation，只有三条全部成功才推进
  指纹，部分发送失败会保留整组重试。独立 GNSS 在无目标定位或绑定错误时仍按 cadence 发布。
- QGC controller 仅在认证、重放和 sequence 门通过后，把 target geolocation 自己的 envelope sequence 与
  `sentAtMs` 注入状态。保存门要求 target 与 GNSS 的 `sentAtMs` 精确相等；新 JSONL 使用
  `multidetect.target-evidence.v3`，同时保存两侧 provenance。v1/v2 保持只读兼容，历史卡片分别标明无质量
  证据、仅同时间质量证据或 v3 同帧绑定。三消息 transport 本身仍明确不声称原子事务。
- focused 回归 `119 passed`，Ruff 与 `git diff --check` 通过；native operator protocol、target evidence store、
  depth-grid 44-fragment Python→C++ HMAC roundtrip、replay RTSP wire self-test 全部退出 0。
- 最终全量回归 3,659 tests：`3657 passed, 2 skipped, 0 failed, 0 errors`，耗时 287.23 秒；JUnit
  533,204 bytes、SHA-256 `A0A328B7814B21C22ECFDCB8530BF097B2CBB00F2BCDD53CF3EBAB68F2132DEC`。
- Windows Release/portable EXE 为 46,603,264 bytes，SHA-256
  `02522C5661803381FD3B8E4E54267CC6BCD17B9769C1C8E2BE00894EBB470CB2`；hidden simple boot 退出 0，
  日志只有既有 Qt FFmpeg 与 point/pixel-size warning，没有 QML 初始化错误。
- `release-parity.json` 为 8,735 bytes，SHA-256
  `83B6B8202924C97A650E8F91253E4CF7020355C6ADAC770B39D6B314A21AA1EE`；UI/task contract 分别更新为
  `AE570E81FE7A31E695D3659869B6B77621CD3052306B260ADC855F55A4BFB1C1` /
  `DA902E0E0E4011545C02C04043369BB02E576AF524CEAD77D2D7858BF82C3CDC`，其余三组身份未变。
- 当前 parity 的 external join/revalidation、三机型 response matrix 与 6 正例/9 故障 campaign 已重算通过；
  新鲜 field-link 仍是 `192.168.1.13/24` 错路由且 Jetson/GR01/RTSP 3/3 超时。E7 manifest/report SHA-256
  分别为 `E2BB25233C8FA77D9945718670FBA3E8CD72E1B7EDD41474FD897E4EC5DF6E8E` /
  `73FE81647CA63AB67DF04F360630D0EE8646AE13B7287E13DF73EE1E3EF0A075`，结果保持
  `gate_passed=false`、`release_allowed=false`、31 blockers。没有修改网络、受保护服务、飞控或载荷；
  室外 GNSS、人工视觉、真实硬件、HIL/REAL 与实飞均未声明通过。

## v153 本轮已收口

- 原生 QGC 的本地目标证据从 `multidetect.target-evidence.v1` 升级到 v2。保存按钮和 C++ durable store
  同时要求：目标坐标/1σ 合格；独立 `GNSS_STATUS` 已认证且状态合格；fix `>=3`、卫星 `>=6`、
  H/V accuracy 分别 `<=6/10 m`、position healthy、reason 为 `gps_qualified`、来源年龄 `<=1.5 s`。
  任一字段缺失、类型错误、非整数 sequence/时间或越界都在写文件前 fail-closed。
- controller 只在原有认证、replay、sequence 门通过后把 envelope `sequence` 与 `sentAtMs` 注入 GNSS
  诊断状态；v2 快照保存这两个字段及 fix、卫星、H/V accuracy、health、reason、source age。它们是与
  坐标快照同时保存的原始诊断 provenance，不把两个独立消息夸大为原子采样绑定。
- 旧 v1 JSONL 保持可读，历史卡片明确显示“旧版 v1 记录，无独立质量证据”；新 native
  `MultiDetectTargetEvidenceStoreSelfTest` 覆盖 v1 兼容、弱 GNSS/字符串布尔零写入拒绝、合法 v2 写入和
  v1/v2 混合重载，退出码 0。operator protocol 与 replay RTSP wire self-test 也退出 0。
- focused QGC 静态回归 `21 passed`，Ruff、compileall 和 `git diff --check` 通过。功能提交为
  `fe39400fc12d0de4e4e57695b465d140303b1296`；最终全量回归 3,658 tests：
  `3656 passed, 2 skipped, 0 failed, 0 errors`，耗时 344.610 秒；JUnit 533,070 bytes、SHA-256
  `5D5A078561A479D9AF278D27D049F202535D03016DB9913E95274018CCC38DFA`。
- Windows Release/portable EXE 为 46,600,192 bytes，SHA-256
  `8225BF9F41426E473F23BC74BF038E576A88EA3556E1A7DD883F1B6C036909BB`；hidden simple boot 退出 0，
  stderr 只有 Qt FFmpeg 与 point/pixel-size warning，没有 QML 初始化错误。
- `release-parity.json` 的 UI contract 更新为
  `6230337df9585b90b256ae64ad016b2397a9f51ddacbaf1bae99acac1c66d14e`；其余四组身份未变。当前 parity
  的 external join/revalidation、三机型 response matrix、6 正例/9 故障 campaign 均重新派生并通过。
- 新鲜 3 样本 field-link 只读复查仍为 `192.168.1.13/24` 路由到目标，Jetson/GR01/相机全部超时；
  `hardware_link_ready=false`。E7 manifest/report SHA-256 分别为
  `71B515320E147F580E84E2AAE20B214B3028642EADF8568BE0FCE757D0F4C45D` /
  `B05C4C5BECE818BEF9DDBD66C389C32059A9695E09DD951E7559886DF99195A9`，结果保持
  `gate_passed=false`、`release_allowed=false`、31 blockers。网络配置、受保护服务、飞控和载荷均未修改。

## v152 本轮已收口

- 新增独立 `GNSS_STATUS`（operator wire type `25`）：Jetson 在目标尚未选择、range 尚未形成时也按默认
  `5 Hz` 发布 GNSS position health、fix type、卫星数、水平/垂直精度、样本年龄和 canonical reason。
  消息刻意不含 latitude/longitude，避免诊断面变成另一条坐标来源。
- domain/codec 对 `fix>=3`、卫星 `>=6`、水平精度 `<=6 m`、垂直精度 `<=10 m`、年龄 `<=1.5 s`
  和 position healthy 使用与 `AdaptiveRangingPolicy.assess_gps_quality` 相同的 fail-closed 语义；伪造
  qualified 的弱 fix 在 Python 和 QGC C++ 两端均拒绝。
- live bridge、MAVLink tunnel 与 UDP session 完成独立 cadence/sequence/peer-change 接入；构建失败只丢弃
  可选诊断包并写 audit，不阻断感知主循环。Jetson 部署验证也检查 type 25 encoder/cadence 是否存在。
- 原生 QGC controller 只在认证、重放和 sequence 门后采纳状态，1 秒无新包即清空；Fly View toolbar
  “室外测距质量”新增 GPS/GNSS 与 GNSS 详情，即使无 target 也能显示精确不可定位原因、fix、卫星数、
  H/V accuracy 和年龄。该链固定 display-only，不开放飞控或物理投放。
- 相关 277 项 Python/QGC 静态回归通过；全仓回归为 `3656 passed, 2 skipped, 0 failed`（3658 tests，
  374.669 s）。Ruff、Ruff format、compileall 与 `git diff --check` 通过；native operator protocol
  self-test 和 replay RTSP wire self-test 均退出 0。release parity 已按当前源码重新生成。
- Windows Release 与 portable EXE 均为 46,587,392 bytes，SHA-256
  `2686D5EFAAB375E08C41C5AD8050C0BB9807C33309F52D1102AB774E96EF61A9`；portable hidden simple boot
  退出 0。构建物路径为
  `D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe`。
- 本轮只关闭软件链“选目标前看不到原始 GNSS 质量”的缺口；人工 Fly View、室外 GNSS 精度、真实
  Jetson/相机/Pixhawk/GR01、HIL/REAL 与实飞仍为 `not_claimed`，E7 继续为 31 blockers。
- 当前 parity 的 external join/revalidation、三机型 response matrix 和 6 正例/9 故障 campaign 已重新
  派生并全部通过；随后用新鲜只读 field-link 报告重算 E7。manifest SHA-256
  `678755799EB890BD650D955F2B811ED2E453FA3D75B4C4C3374E55F729157A82`，report SHA-256
  `A5947B5B9ED0837C2D65161A18CD83D26FCA06D5FE67D27B135377AF4EC50876`，结果仍是
  `gate_passed=false`、`release_allowed=false`、31 blockers。

## v151 本轮已收口

- 对当前台式机执行 3 样本、协议级、路由级 field-link 只读预检。18,143-byte 报告 SHA-256
  `976B0F3E17214274EC79E62F556F5BB00A8F89A37BDC02A7D088B9893269ACC3`；4,378-byte 接口诊断 SHA-256
  `380C2B9F7C246A42A95DB14F7285B53B0B0C44233D04EB054A0508511D43BA95`；独立 verifier 以
  `--allow-unready` 退出 `0`，证明报告结构与哈希链可读，不代表硬件 ready；
- 3/3 样本的 Jetson SSH、GR01 MAVLink 和相机 RTSP 传输/协议探测全部超时；操作系统为三个端点选择的源地址均为
  `192.168.1.13`，不属于要求的 `192.168.144.0/24`。因此 `route_checks_passed=false`、
  `hardware_link_ready=false`，接口诊断也明确 `selection.status=missing` / `candidate_count=0`；
- 用该新鲜现场失败证据和 v150 当前 parity 派生证据重绑 E7：manifest SHA-256
  `9B5C614BE53793E15D5086FA44301BC99530E39DD791D662FFBF642FF055718D`，report SHA-256
  `BD102087CEA1095796A188C79D0A467847BCE987347D6C945264FACC35321D5B`。E7 仍为
  `gate_passed=false` / `release_allowed=false`，阻断项从 32 降至 31；只消除了旧现场报告过期阻断，
  `real_field_link.passed=false` 没有提升；
- 本轮没有修改网络配置、飞控或载荷，没有修改 C++/QML，因此无需重建 EXE。版本仍为 `0.3.0-pre.8`；硬件、
  HIL/REAL、室外 GNSS 与飞行验收继续保持 `not_claimed`。

## v150 本轮已收口

- 新增 GR01 review trust registry schema v1、实现与 repository wrapper；registry 用独立渠道保存的文件 SHA-256 作信任锚，
  为 `source_operator` / `reviewer` 登记 principal ID、identity、key ID、Ed25519 公钥、全局/逐 key 有效期及吊销状态；
- portable review schema 升级到 v5；`review` / `verify` 不再接收裸公钥作为信任根，而是重开同一 registry 并核对包外
  SHA-256。operator key 在 source attestation 时点和 review 时点都必须有效，reviewer key 在 review 时点必须有效；
  未来 attestation、过期/吊销 key、identity/key/public-key mismatch 以及角色复用均 fail-closed；
- clean feature commit `fb5e8e4069a35dc53b010c48e4ee6c1bc70084f4` 的 trust-registry + review 软件 smoke
  `22 passed`；JUnit 3,267 bytes、SHA-256 `771C2CBB3DABEF6DBE14BDBFF7DA867D2D2E4D867A6D6F202EEF0A29203369BF`；
- safety parity 更新为 `E179F406...FE0030DE`；external join/revalidation、三机型 matrix 与 6 正例/9 故障 campaign
  已按当前 parity 重算并通过。E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01
  `present=false/passed=false`。最终全量回归 3,652 tests：`3650 passed, 2 skipped, 0 failed, 0 errors`，耗时
  276.482 秒；JUnit 532,203 bytes、SHA-256 `213DC40ADE45E01502E96B8E284DBE1E8FB77068819D105B45A3745467FE601B`。
  真实 registry 分发、真人身份映射、第二机复核、现场 GR01 session、HIL/REAL 与飞行验收仍待完成。

## v149 本轮已收口

- 新增 source attestation schema v1：现场 operator 用独立 Ed25519 key 签署 inventory 的 bytes/SHA-256/canonical hash、
  bundle ID 与 bundle-content hash；seed 只从环境读取，detached attestation 强制写在 inventoried session 目录外；
- portable review schema 升级到 v4：第二机必须用包外预登记 operator 公钥验源，再由 reviewer key 签署 review；operator/
  reviewer 的 identity、key ID、公钥三项必须全部分离，错误 operator 公钥、source signature 篡改和角色复用均零输出失败；
- claims 只证明两把指定 key 的 possession、source inventory 和 review receipt 完整性；两个人的现实身份仍固定为未验证，
  target environment、完整 E7、网络、硬件、HIL/REAL 与飞行验收均未被软件 fixture 提升；
- clean feature commit `4e3585c74857a2f156e1baab3f797418218cb95f` 的双签名软件 smoke `6 passed`；JUnit 1,127 bytes、
  SHA-256 `7CFE1EFE6F145665422C548A01BF4FBF3AF983BDF51D90705995821DE61A324F`；
- safety parity 更新为 `B900403A...936B6F35`；external join/revalidation、三机型 matrix 与 6 正例/9 故障 campaign
  已按当前 parity 重算并独立通过。E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01
  `present=false/passed=false`。最终全量回归 3,645 tests：`3643 passed, 2 skipped, 0 failed, 0 errors`，耗时
  287.945 秒；JUnit 531,218 bytes、SHA-256 `931052CC4487CBD16DD9CFE9C8676D6271761ABD2CA0CE350BAB485823E61050`。
  真实 operator/reviewer 公钥登记、真实第二机双签和现场 GR01 session 仍待完成。

## v148 本轮已收口

- portable review schema 升级到 v3：第二机必须从环境读取 32-byte Ed25519 seed，receipt 绑定 reviewer、key ID、
  public key、signed-payload SHA-256 和 64-byte signature；私钥不会进入命令、回执或日志；
- 独立 `verify` 不信任 receipt 内嵌 key，强制使用包外固定公钥；错误公钥、signature tamper、claim tamper、缺失或
  非法 seed 均 fail-closed，其中 key 错误在任何 transfer/review receipt 写入前零输出；
- claims 明确 `reviewer_key_possession_verified=true`、`reviewer_human_identity_verified=false`，只证明密钥持有和
  回执完整性，不伪造真人身份、target environment、完整 E7、网络、硬件或飞行验收；
- clean commit `1bcd492d08e1e9a61b1b93659a6f4a3c376800bf` 的软件签名 smoke `5 passed`；JUnit 1,009 bytes、
  SHA-256 `F93E70227DA0C83BEE6F24DB96A81E012C46C4A88A1A5E97ED26702208A9BF2F`；
- safety parity 更新为 `F86FF499...97FA569F`；external join/revalidation、三机型 matrix 与 6 正例/9 故障 campaign
  已按当前 parity 重算并独立通过。E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01
  `present=false/passed=false`。最终全量回归 3,645 tests：`3643 passed, 2 skipped, 0 failed, 0 errors`，耗时
  270.522 秒；JUnit 531,218 bytes、SHA-256 `144ACD03F4D678CD9E9A9D6E3631570D438D993F7934A3D054F6AA2076A25209`。
  真实 reviewer key 登记、真实第二机签署与现场 GR01 session 仍待完成。

## v147 本轮已收口

- portable review schema 升级到 v2，`review` 必须显式接收独立渠道保存的 inventory SHA-256；实际 inventory
  哈希在任何 transfer/review receipt 写入前核对，mismatch 时零输出并 fail-closed；
- receipt 绑定 expected/actual inventory SHA-256 与 `inventory_hash_matches_expected=true`，独立 verifier 重算；
  现场 producer 在 snapshot 后打印可信哈希，必须与 session 复制介质分开保存才构成包外信任锚；
- 该 SHA-256 不等于数字签名或操作者身份认证，也不重验目标环境、完整 E7 传递来源、网络或硬件。clean commit
  `089028559e827e63dbb2008536920ccf0c2c6a2c` 的软件 smoke `2 passed`；JUnit 498 bytes、SHA-256
  `94231E01A2881164580802F3BC6BD95E68D8185C19B891EA007A5E3FB1C4F39B`；
- safety parity 更新为 `8BA76D61...ADBD4A0D`；external join/revalidation、三机型 matrix 与 6 正例/9 故障 campaign
  均重算并独立通过。E7 保持 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01
  `present=false/passed=false`。最终全量回归 3,643 tests：`3641 passed, 2 skipped, 0 failed, 0 errors`，耗时
  272.363 秒；JUnit 530,841 bytes、SHA-256 `8DA38949FA44806ABE294DBA2FC051500557DBD75CB83A3B9A09CE7B19DC903E`。
  真实 GR01 session、独立渠道哈希传递、第二机 review/签字仍待现场完成。

## v146 本轮已收口

- 新增 `gr01_release_review.py`、repository wrapper 与 closed review schema：现场 final record 先经过原有强 verifier，
  再把 plan、handoff、bench、E7 manifest/report 与 record 共 6 文件写入逐字节 transfer inventory；
- `run_gr01_release_session.ps1` 只有在五源 record 独立验证成功后才执行 `snapshot`，因此输出目录直接具备跨机搬运清单；
  inventory/transfer/review 输出均 write-new，复制包拒绝增删改、symlink、路径逃逸和 case collision；
- 第二机 `review` 不依赖 origin/target 绝对路径或现场 secret：重开五源 hash/bytes，执行 portable handoff verifier、
  GR01 bench verifier，核对 plan→handoff→bench/E7 配置、target Git/parity 投影和 manifest/report GR01 绑定；专门回归
  证明单独篡改 record projection 后即使重建通用 inventory，内部五源投影不一致仍 fail-closed；
- review receipt 固定 `target_environment_reverified=false`、`canonical_e7_sources_recomputed=false`，并保持
  no-hardware/no-flight/no-control/no-payload。clean commit `da585dc14be11cb6f383f16b3eb06ac56349146e` 的软件跨机
  fixture 通过；JUnit 356 bytes、SHA-256 `F26FBD3750858FEDB603FC818C6354829D8DA34F009B9EC46B7E4D9AC83E225F`；
- safety parity 更新为 `2E3B5C4A...87A6A3C`；external join/revalidation、三机型 matrix、6 正例/9 故障 campaign
  均重算并独立通过。E7 保持 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01
  `present=false/passed=false`。最终全量回归 3,642 tests：`3640 passed, 2 skipped, 0 failed, 0 errors`，耗时
  267.29 秒；JUnit 530,699 bytes、SHA-256 `3C38DCAAA153B253080834D357BB1856073EAA4661BE037CEC0F26314348D194`。
  该 fixture 没有连接网络/硬件，不构成 GR01 或第二台真实现场机验收。

## v145 本轮已收口

- `run_gr01_release_session.ps1` 新增 `-SourcePlan`，可把开发机预生成 plan 逐字复制到现场 session；输入必须为
  regular non-reparse file，复制前后 SHA-256 必须一致，`-PlanOnly -SourcePlan` 冲突在创建 artifact 前拒绝；
- handoff consumer 新增 `match` 门：先按 `--check-environment --require-ready` 严格复验目标 source/secrets，再将
  source plan 的 endpoint、hardware ID、canonical CIDR、五项阈值、两个 secret 变量名和 configuration digest 与现场
  CLI 参数逐项对账；该门固定在 GR01 bench/UDP 前，任何 drift 均 exit 1；
- clean commit `e1a821ee7a7265979d63cfeb1dbce1da743029b4` 无网络导入 smoke：origin plan 3,059 bytes、
  SHA-256 `02F4A3C4430ECB277B56B6889A12010A8B1E5E37EC1F29DABDE1B8016B5456A2`；target handoff
  3,109 bytes、SHA-256 `1325C9F19DF0F5A51A2B20D4246A168648BD5B0D4ECFD0C3E6865EFE263ED116`；
  故意把现场端口从 14580 改为 14581 时 wrapper exit 1，目录只有 plan/handoff、没有 bench；原参数 match exit 0；
- safety parity 更新为 `64628035...517A6`；external join/revalidation、三机型 matrix、6 正例/9 故障 campaign 均重新
  绑定并独立通过。E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01
  `present=false/passed=false`。最终全量回归 3,637 tests：`3635 passed, 2 skipped, 0 failed, 0 errors`，耗时
  287.622 秒；JUnit 530,041 bytes、SHA-256 `89F04B8656D485F942AAC9B0260C2712FEE8BB1255E401EC6A09A01957C6A7F8`。
  smoke 使用临时占位 secret，仅验证软件门，不接触网络/硬件，不构成 GR01 验收。

## v144 本轮已收口

- GR01 final session 升级为 schema v3，将 source plan、target handoff、hardware bench、E7 manifest 与 E7 report
  作为五份 distinct/hash-bound source；最终 record 新增 target host/repo/Python/output、configuration digest、Git HEAD、
  source-equivalent、ready、environment-reverified 与 plan/bench/E7 configuration-match 投影；
- consumer 不再把 origin absolute paths 当目标机执行条件，而是严格运行 handoff verifier 的
  `--check-environment --require-ready` 等价语义，重开 handoff 指向的同一 plan，并对账 plan SHA/bytes、Git、release
  label、safety hash 与现场配置；origin plan 可以 non-ready，只有目标 handoff ready 才可授权执行；
- `run_gr01_release_session.ps1` 在任何 bench/UDP 前按 plan → handoff create → handoff strict verify 顺序执行，新增
  `TargetHostId`，最后显式把 `--handoff` 传给 session creator；handoff 或五源任一漂移均不能生成成功 record；
- clean commit `a4d43234fec86f1d587e3e0e62ef6de9b76c44a0` 的无网络 smoke 为 source-equivalent/structurally valid，
  两个现场 secret 缺失使 handoff non-ready；专门 relocated-origin 五源测试通过，但它使用软件 fixture，不是硬件 session；
- 新 parity 下 external join/revalidation、三机型 reported-position matrix 与 6 正例/9 故障 campaign 独立通过；E7
  保持 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01 `present=false/passed=false`。
  最终全量回归 3,633 tests：`3631 passed, 2 skipped, 0 failed, 0 errors`，耗时 249.884 秒；JUnit
  529,356 bytes、SHA-256 `7CA6FF563DD7F2993CC364972BCF026CBC71CBF55AB588FB91778494E52B9C7C`。
  本轮不改 C++/QML，不重建 EXE，不产生网络、硬件、射频、HIL/REAL、飞行或最终发布声明。

## v143 本轮已收口

- 新增 `gr01_release_handoff.py`、repository wrapper 与 closed schema，把 source plan 转换成目标机专属、
  write-new 的 portable handoff receipt；来源 plan 可保留另一台机器的绝对执行路径，不再要求两机 checkout 同路径；
- handoff 在目标机重算 tracked-clean、Git HEAD、canonical release parity、四个执行工具身份和 parity source
  verification；只允许目标 output 位于目标 repository 内，源码或工具不同即 `source_equivalent=false`；
- source plan repository/parity/tool identity 现在逐字段闭合验证；非法 Git/SHA/path/bytes/type 在写 receipt 前直接拒绝；
- receipt 绑定 source plan 的相对路径/bytes/SHA、portable configuration digest、目标 repo/Python/output 路径、
  host ID 和两个 secret 的名称/present；不序列化 secret 值，固定 no-network/no-hardware/no-control claims；
- clean commit `3b8a9d5ac4ab9a2f4d7206dd2f25a950c76ba2b5` 的无网络 smoke 得到
  `source_equivalent=true`、结构复验 exit 0；两个现场 secret 缺失使 `ready_to_execute=false`，严格复验 exit 1。
  plan 3,021 bytes、SHA-256 `71FC66BC2F8614CA5021AE2CA9A636848475C5237A6A6A1D0E0E3D62DC86F58B`；handoff
  3,288 bytes、SHA-256 `1FF368668E757747B50D2CD18F308D3CB26E4C692A3090AAF6075B8234B8E962`；未接触网络或硬件；
- 新 parity 下 external join/revalidation、三机型 reported-position matrix 与 6 正例/9 故障 campaign 均独立通过。
  E7 继续 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01 `present=false/passed=false`。
  最终全量回归 3,631 tests：`3629 passed, 2 skipped, 0 failed, 0 errors`，耗时 277.38 秒；JUnit
  529,121 bytes、SHA-256 `B68E93EEA55B0631B6D11179F634FB25361E53C186660276329BF0C0281025D4`。
  本轮不改 C++/QML，不重建 portable EXE，不产生硬件、射频、飞行或最终发布声明。

## v142 本轮已收口

- GR01 final session 升级为 schema v2，并将 bench 前通过的 `gr01-release-session-plan.json` 作为第四份
  write-new 来源绑定；record 保存 plan 相对路径、bytes/SHA-256、Git HEAD、release label、safety hash 和 ready/source 状态；
- session consumer 重开 plan 并运行 canonical verifier，要求 source-bound plan 本身 ready；随后逐项对比 plan 与实际
  bench/E7 的 host、port、hardware ID、source CIDR、round trips、loss、P95、retry 和 evidence max-age；
- PowerShell producer 在创建最终 record 时显式传入 `--plan`；plan、bench、manifest、report 任一漂移，或计划参数与
  实际证据不一致，都不能生成/复核成功 session；新增 configuration-drift 回归；
- 干净提交 `46845165b0d6fc870399406bcd1614defc4d71fb` 无网络 smoke 的 Git/parity/tool 来源均通过，两个
  secret 缺失使 `ready_to_execute=false`；plan 为 3,021 bytes、SHA-256
  `0C6520FB0CB71B579141BBEEE4B62044A7FEC43EDCF5090945BAA96230E3CAB5`，未接触网络或硬件；
- canonical parity 为 7,857 bytes、SHA-256
  `70FB339ACF17BB1FF61008C4B323422278F6F606C0CD2698B54982E38E1FC87D`，safety hash 为
  `0F797612E61B2ECBB798849BCD8CAE5A00EDAFDDA1936193C27B37B1039B1F51`；四份派生证据独立通过。
  E7 继续 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01 `present=false/passed=false`。
  最终全量回归 3,623 tests：`3621 passed, 2 skipped, 0 failed, 0 errors`，耗时 159.54 秒；JUnit
  527,922 bytes、SHA-256 `1FA7FCD5F6320B819F7AAA1C0A3E833FFB99D2C5AE4BF0D0035E953B46B88C46`。
  本轮不改 C++/QML，不重建 portable EXE，不产生硬件、射频、飞行或最终发布声明。

## v141 本轮已收口

- GR01 plan 升级为 schema v2：除现场参数和 secret presence 外，新增 Git HEAD、tracked-dirty、canonical
  `release-parity.json` 来源复算，以及 producer、CLI shim、独立 verifier、plan module 四个工具的 bytes/SHA-256；
- 新增 `gr01_release_plan.py` 与 installed-tree-independent verifier。verify 会重开工具和 parity，重新计算来源身份；
  `--check-environment` 复核 secret 是否仍存在，`--require-ready` 同时拒绝 diagnostic/non-ready plan；
- 实际 session 在任何 UDP bench 之前先 write-new 生成 plan，并立即执行
  `verify --check-environment --require-ready`。源码 dirty、parity stale、工具漂移或 secret 缺失都会在网络前失败；
- 干净提交 `abca8465d53ea525b545380ff06e40ca922a81da` 的无网络 smoke 成功复核来源，但因两个 secret 均缺失而
  `ready_to_execute=false`；计划 SHA-256 为
  `2F88627DFBA4FA02014EF15B3768583BB347081BE3FE0E3A6B85D8961B5D1332`，未接触网络或硬件；
- canonical parity 为 7,857 bytes、SHA-256
  `4C62CE8B5E2A6687F9E29DEA5EC28888B4635BB508891AF7A247CE1CCC9D7F8B`，safety hash 为
  `C05D32D7EF7F37A72E03001BE86DD190D56930A48926BE97CC2B78DF9942C8F8`；四份派生证据独立通过。
  E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01 `present=false/passed=false`。
  最终全量回归 3,622 tests：`3620 passed, 2 skipped, 0 failed, 0 errors`，耗时 155.35 秒；JUnit
  527,791 bytes、SHA-256 `05D64EBDA25BDFAA8F26DE5AA780B9070A5DD36B2AF3167231DD1C31E4DC8F35`。
  本轮不改 C++/QML，不重建 portable EXE，也不产生硬件、射频、飞行或最终发布声明。

## v140 本轮已收口

- `run_gr01_release_session.ps1` 新增 `-PlanOnly/-PlanOutput`。预演验证 non-loopback literal IPv4、source CIDR、
  hardware ID、阈值、repo-contained 输出路径、Python/CLI 可用性和两个 secret 环境变量的“名称+是否存在”；
  secret 值不进入命令参数、输出或日志；
- plan 使用 write-new JSON；closed schema 固定四步后续操作、`network_contacted=false`、
  `hardware_observed=false`、`hardware_flight_acceptance=not_claimed` 和全部 no-control/no-release 位。缺 secret 时
  正常生成 `ready_to_execute=false` 的诊断回执，不启动 bench、不创建 session 目录；
- 新增 repository-source CLI shim，现场 producer 不再依赖 editable-install；shim、producer 与 plan schema 均进入
  `safety_policy` parity。canonical parity 为 7,758 bytes、SHA-256
  `4FB09C34E7925D43B3A55926FF543D8A42716320C613E71948D2A2BE8AA70402`，safety hash 为
  `C3F6D82749779D8AD1E7484AC237DC2E92078C396F75288BAB476C7969AFD059`，其余四组不变；
- 新 parity 下 external join/revalidation、三机型 reported-position matrix 与 6 正例/9 故障 campaign 均独立通过；
  E7 继续 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01 `present=false/passed=false`。
  最终全量回归 3,616 tests：`3614 passed, 2 skipped, 0 failed, 0 errors`，耗时 204.33 秒；JUnit
  527,019 bytes、SHA-256 `9E65D248BA8167721784769AF91BA3417D3CF106A0EEDF5CA78C47EF0A46EB8E`。
  全仓 Ruff、82 份 schema、源码 compileall 和 diff check 通过。本轮没有接触硬件，没有修改 C++/QML，
  不重建 portable EXE。

## v139 本轮已收口

- 新增 `scripts/run_gr01_release_session.ps1`：真实现场只需一个命令，即按固定顺序执行 hardware GR01 bench、
  `--require-passed` 独立复核、freshness-bound E7 manifest/report 和 session consumer 复核；每轮输出目录及最终
  `gr01-release-session.json` 均拒绝覆盖，任一步失败都不会出现成功记录；
- secret 只通过两个环境变量名称传入，值不进入命令行、JSON 或日志；host、port、hardware ID、expected source
  CIDR、样本数、丢包、P95、retry 与最大证据年龄均显式绑定，受控 release-gate 参数不能被 extra args 覆盖；
- 新增 `gr01_release_session.py`、独立入口与 closed schema。consumer 重开 bench/manifest/saved report，重跑
  GR01 verifier 和 canonical E7 evaluator，核对 source SHA/bytes、freshness、GR01 子门投影及 no-control/no-release
  claims；record 被篡改或任一来源漂移都会 fail-closed；
- producer、consumer、schema 与入口全部纳入 `safety_policy` parity。canonical parity 为 7,655 bytes、SHA-256
  `E138A41DE32CAD23DE554EB0AC21AE95C4D7B004498509CB434CAEEF44400575`；仅 safety hash 更新为
  `AE4636175DA3603C23F58500DD7FC7BAFF619CD533D07E60CBA8222E968BB7CC`，其余四组不变；
- 新 parity 下三机型 external join/revalidation、reported-position matrix、6 正例/9 故障 campaign 均独立通过。
  E7 重算仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，GR01 仍
  `present=false/passed=false`；没有运行真实 session、没有硬件/飞行声明；
- 最终全量回归 3,615 tests：`3613 passed, 2 skipped, 0 failed, 0 errors`，耗时 154.16 秒；JUnit
  526,870 bytes、SHA-256 `C9F8B4ADC5B826C499851DD6A69A6E27F864FC8E9756068318D71427FC84DEF8`。
  全仓 Ruff format/lint、仓库源码 compileall 与 diff check 全部通过。本轮未改 C++/QML，portable EXE 不重建。

## v138 本轮已收口

- `real_gr01_operator_link` 新增 `observed_at_utc`、`report_timestamp_epoch_s` 与 `max_age_s` 三字段原子绑定；
  writer 默认只接收 900 秒内的报告，拒绝过期、无时区、非有限时间和超过 5 秒容差的未来时间；
- evaluator 使用整个 E7 报告共用的 canonical `evaluated_at_utc` 重算 age，时间字符串与 epoch 不一致、投影缺一、
  源报告时间漂移、复评时已过期全部 fail-closed；历史 prerelease 记录仍可读取，但最终 `0.3.0` 明确要求 freshness
  三字段完整存在，不能永久复用旧台架报告；
- Windows wrapper 新增 `-RealGr01OperatorLinkMaxAgeSeconds`（默认 900，范围 0..86400）并传给 manifest CLI；部署
  证据身份现在由 hardware ID、non-loopback endpoint、expected source CIDR、实际 source address 和采集时间共同组成；
- release parity 仍为 7,454 bytes，SHA-256
  `5179C34077DC61CF0A0BBEB530AA8A08A28CEB53FC327E5CADB66D70FD9A8723`；release-gate schema 变更令
  PX4 Mission path hash 更新为 `91CFD7816000A26BF04BEA11703247DDED3A4B07AAC7797CEBC9BD5161E746E5`，其余四组不变；
- 三机型 external join/revalidation、reported-position matrix 与 6 正例/9 故障 campaign 均从不可变 source 按新
  parity 重算并独立通过。E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，且
  `real_gr01_operator_link.present=false`，没有伪造现场报告；
- 首轮 strict 全量回归发现 annotation UI 测试的 `Popen` stdout/stderr 管道未关闭，产生 1 个
  `ResourceWarning`；测试改用 `communicate()` 并含 timeout/kill 回收，连续定向复测通过。最终全量 3,608 tests：
  `3606 passed, 2 skipped, 0 failed, 0 errors`，JUnit 238.449 秒、525,966 bytes、SHA-256
  `3EE31F54061D0CDC7F976687F87FA47B67B342907C6C38E8727D6D7513575B75`；Ruff format/lint、compileall、schema
  与 diff check 全部通过。本轮未改 C++/QML，portable EXE 不重建。

## v137 本轮已收口

- E7 新增独立 `real_gr01_operator_link` 顶层记录；manifest writer 只接受通过 strict verifier 的
  `gr01_bench_passed` 硬件报告，并绑定报告相对路径、SHA-256、字节数、GR01 身份、non-loopback endpoint、
  expected source CIDR、实际 source address、ACK/timeout/attempt 守恒、时延/丢包、HMAC、MAVLink2 签名、
  双向确认与全部 no-control 位；
- evaluator 每次从 manifest 重新打开源报告、复核哈希/字节数、重跑 canonical verifier，并逐字段对账；未知
  字段、投影篡改、源文件漂移和软件 loopback 冒充 REAL 都 fail-closed；最终 `0.3.0` release channel 要求该门
  单独存在且通过，不能由 `real_alert_link` 告警 ACK bench 替代；
- `run_current_release_gate_pre8.ps1` 新增可选 `-RealGr01OperatorLinkBench`；当前没有真实 GR01 bench，因此
  current manifest 明确 `present=false/passed=false`，没有生成硬件通过声明；该要求只在最终 release channel
  强制，当前 prerelease E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers；
- release-gate schema 进入 PX4 Mission parity，当前 parity 文件 7,454 bytes、SHA-256
  `74854DE43D5BF772AAAFE9DA4B7359603BA9FC7F20B37F9612772EE1D9CCCC09`，PX4 hash 为
  `AA38DB69E111FB429DABCE57CF4585D9069F172FBE3759321EF843F5F45808A7`；三机型 external
  join/revalidation、reported-position matrix 与 6 正例/9 故障 campaign 已按新 parity 重算并独立通过；
- 最终全量回归 3,604 tests：`3602 passed, 2 skipped, 0 failed, 0 errors`，JUnit 时间 240.937 秒、
  525,403 bytes、SHA-256 `669E08AE1821BEB602B45651558DFB101B83F2D3BB9A2EADA3315C2C4600E86A`；
  全仓 Ruff format/lint、compileall、schema 和 diff check 通过。本轮未改 C++/QML，portable EXE 不重建。

## v136 本轮已收口

- GR01 目标选择/ACK hardware bench 现在强制 `--expected-source-cidr`；CLI 先读取 OS route 选择的
  canonical IPv4，再把同一地址显式 bind 到实际持续 UDP session。源地址不在声明网段时，往返循环不启动，
  `transmission_attempts=0`；
- 新增 strict `gr01-link-bench` schema 与离线 verifier，独立重算 endpoint、source route、ACK/timeout/attempt
  守恒、丢包、P50/P95 形状、阈值、HMAC/MAVLink2/双向标志、结果字段和全部 no-control 位；伪造 route flag、
  计数或 `passed` 不能通过；
- `operator_udp.py`、GR01 bench、CLI、schema 和 verifier 均进入 `safety_policy` parity。Phase 6 的软件验收契约
  已完成，但“经真实 GR01 双向 IP 实测”继续未勾选；该变更没有发送飞控或载荷指令，也不构成射频/现场通过；
- 首次全量回归发现且只发现进度 JSON 尚未同步新 parity（`3596 passed, 2 skipped, 1 failed`）；该文档一致性
  失败已按设计阻止虚假收口；同步后最终全量回归 3,599 tests 为
  `3597 passed, 2 skipped, 0 failed, 0 errors`，耗时 250.04 秒。JUnit 524,720 bytes、SHA-256
  `68467B9949C7CA0A5203045F26C37682839A6D93713ECD59115FE450D0C8D7C2`。
- 新 safety parity 下已从原始不可变 source 重算三机型 external join/revalidation、reported-position matrix 与
  6 正例/9 故障 campaign，四项 verifier 均通过；E7 重新生成后仍为 `gate_passed=false`、
  `release_allowed=false`、32 blockers，GR01/G20、现场数据和人工验收没有被软件证据替代。

## v135 本轮已收口

- 新增 strict `alert-link-topology` 契约，将 `gr01_direct` 与 `v6x_fallback` 明确区分；直连 endpoint 必须位于
  声明 source CIDR，所有地址采用 canonical non-loopback IPv4，任何控制/投放位都固定关闭；
- V6X 回退必须同时提供并逐字节 SHA-256 复核 GR01 直连失败、PX4 SITL 路由、拆桨 V6X 台架三份相对路径证据；
  缺失、越界、不可读或哈希漂移均在 UDP 发送前失败；
- alert-link bench 升级 v5，`--topology` 原子提供 endpoint、port、sender/receiver、hardware ID 与 source CIDR，
  冲突 CLI 参数会被拒绝；报告内嵌规范化 topology 和 canonical SHA-256，独立 verifier 重算绑定；
- release manifest 只接受 v5 REAL alert-link 报告，历史 v1-v4 仍可独立复核；topology 源码/schema 已加入
  `safety_policy` parity。该软件契约没有证明 GR01/V6X/G20 现场链路已通。
- 最终全量回归 3,592 tests 为 `3590 passed, 2 skipped, 0 failed, 0 errors`，耗时 242.489 秒；JUnit
  523,829 bytes、SHA-256 `7DBB937B24033EBB17F1BFCFA862E1A651624F7F65D5265A038046EE432168A5`。
- 新 parity 下重算的三机型 external join/revalidation、reported-position matrix、6 正例/9 故障 campaign
  均经独立 verifier 通过；E7 重跑保持 `gate_passed=false`、`release_allowed=false`、32 blockers。

## v134 本轮已收口

- `AlertDeliveryReceipt` 新增 `source_address`；真实 UDP transport 在 bind/connect 后、send 前读取 socket
  `getsockname()`，规范化为 IPv4，并在它与配置 `source_host` 不同时直接失败；
- retry publisher 暴露最后一份已通过 receipt 校验的结果；alert-link bench v4 保存每个成功 ACK 的实际 sender
  source、观察数和完整性。hardware 只有全部成功样本都观察到同一 route/bind 地址才通过；
- 新 release manifest 只接受通过的 v4，历史 v1/v2/v3 仍可独立复核。当前错网段负测仍为 0 transmission，
  v4 另明确记录 0 个运行时 source observation；
- 新 parity 下 external join/revalidation、三机型 reported-position matrix 和 6 正例/9 故障 campaign 均已重算并
  独立复核；E7 仍为 `gate_passed=false`、`release_allowed=false`、32 blockers。
- 最终全量回归 3,584 tests 为 `3582 passed, 2 skipped, 0 failed, 0 errors`，耗时 242.754 秒；JUnit
  522,614 bytes、SHA-256 `6735DE1B6A3FB85351D17F46CA9BAFECEE09DE1978F6E791D6D365663BC7C71C`。

## v133 本轮已收口

- `UdpAcknowledgedAlertTransport` 新增 canonical IPv4 `source_host`；配置后每次发送使用的实际 UDP socket 都先
  `bind(source_host, 0)`，不再只依赖另一个预检 socket 对下一跳路由的预测；
- alert-link bench 升级 schema v3，同时记录 `transport_source_address` 与
  `transport_source_address_bound`。hardware 只有 route source 属于 expected CIDR 且 sender socket 固定同一地址
  才进入 ACK loop；v1/v2 历史报告仍可复核，新 release manifest 只接受通过的 v3；
- 当前控制机真实负测的 route/transport source 均为 `192.168.1.13`，不属于 `192.168.144.0/24`，所以 bench
  退出 1、离线 verifier 退出 0、`transmission_attempts=0`；这不是实链通过；
- 新 parity 下 external join/revalidation、reported-position matrix、6 正例/9 故障 campaign 均重算通过；E7
  重跑保持 `gate_passed=false`、`release_allowed=false`、32 blockers。
- 最终全量回归 3,582 tests 为 `3580 passed, 2 skipped, 0 failed, 0 errors`，耗时 242.077 秒；JUnit
  522,327 bytes、SHA-256 `4E61645B598CA28AAF1A8BEEF13C7130C73E6CFD31D75B9F4C9A5D80FDD6853A`。

## v132 本轮已收口

- authenticated alert-link bench 升级为 schema v2；`--hardware-mode` 除 non-loopback endpoint 和 hardware ID
  外，必须显式提供 `--expected-source-cidr`，报告保存 canonical CIDR、OS-selected local source address 与
  `source_route_verified`；
- route selection 使用未发送数据的 UDP connect/getsockname，在第一个认证告警前完成。源地址缺失、无法解析或
  不属于指定网段时不进入 ACK 循环，`transmission_attempts=0`，但失败报告仍能由 strict verifier 重算；
- 当前控制机负测实际得到 `local_source_address=192.168.1.13`，不属于 `192.168.144.0/24`；bench 退出 1、
  离线 verifier 退出 0（合法失败）、0 transmission。它只证明 fail-closed，不是 GR01 实链证据；
- release manifest writer 只接收 v2 REAL bench，并 hash-bind source CIDR/address/result；evaluator 继续读取历史 v1
  记录。alert transport、bench 与 schema 新增到 `safety_policy` parity；全量回归 3,577 tests 为
  `3575 passed, 2 skipped, 0 failed, 0 errors`，耗时 264.26 秒。现场 ACK/GR01/V6X/G20 仍未通过。
- 新 safety/PX4 parity 下已从既有不可变 source 重算三机型 external join/revalidation、reported-position matrix
  与 6 正例/9 故障 campaign，四项 verifier 均通过；E7 manifest/report 随后重跑，仍明确为
  `gate_passed=false`、`release_allowed=false`、`release_blocked_reasons=32`，没有把软件派生证据提升为现场验收。

## v131 本轮已收口

- 在当前 Git 对象上创建独立 detached worktree 后发现 Windows checkout 会把 204 个 QGC Python 文件恢复为
  CRLF，使主工作树已通过的 Ruff baseline 无法复现；新增 `.gitattributes` 对 `*.py`/`*.pyi` 强制 LF。第二个
  全新检出实测 1,524/1,524 format check、lint、compileall 和零 diff 全部通过；
- manual validation 原先只安装 `.[dev]`，干净环境会先缺 `pymavlink`，随后缺 OpenCV/NumPy/ONNX/requests。
  workflow 现安装 `.[dev,vision,pixhawk,security,model-tools]`，`requests>=2.32` 已进入 dev extra 与 `uv.lock`；
- `artifacts/` 按仓库政策不进入 Git。依赖真实本地 replay/evidence/隔离候选模型的测试现在只在对应文件存在时
  执行；干净检出中以带原因的 skip 保留边界，既不报假 FileNotFoundError，也不把未运行的资产集成检查记作 pass；
- VTOL capture ready marker 现在用已序列化的 6 位 monotonic 时间作为内部比较基准，消除了 exact echo 因小于
  1 微秒 round-down 被拒绝的跨解释器不确定性；该测试在 Python 3.13.7 下连续复现失败后已修复并通过；
- Windows Python 3.13.7 新 venv 按 manual workflow 顺序完成 editable install、`pip check`、compileall、全套
  pytest、Ruff lint/format：3,574 tests 为 `3559 passed, 15 skipped, 0 failed, 0 errors`，耗时 218.17 秒；
  JUnit 524,739 bytes、SHA-256 `864688E1B025B104E0F281D0A329ADE959C6B55BEBDAD5237CC12CA39B8C1790`。
  15 个 skip 均为 Windows 能力或 Git 外本地资产边界；GitHub-hosted 3.11/3.12/3.13 仍待手动运行。
- 主工作树 Python 3.12 保留本地 replay/evidence/quarantined ONNX candidate 后再次跑满 3,574 tests：
  `3572 passed, 2 skipped, 0 failed, 0 errors`，耗时 239.95 秒；JUnit 521,236 bytes、SHA-256
  `F7E7EED42C151B7D987A5554B035B17E48ABA13167254BF0A1F9B9A8DD037084`。这证明新增 skip 条件不会绕过
  资产存在时的严格集成检查。

## v130 本轮已收口

- Ruff 0.16.4 首次扫描 433 个未格式化文件（ground-control 205、src 106、tests 98、scripts 24）；全仓
  格式化后 282 个文件形成 canonical Git 差异，其余为换行归一。3 个 formatter 产生的 101 字符生成式已做
  等价拆行；最终 `python -m ruff format --check .` 为 1,523/1,523，lint、compileall 和 diff check 通过；
- `release-parity.json` 随 4 个受影响契约组重建，UI contract 未变。由六份不可变外部 runtime/AI source
  重算三机型 join/revalidation，由既有真实 PX4/Gazebo source 重算 reported-position matrix 与 6 次正例、
  9 次故障 campaign；四份派生证据均经独立 verifier 通过，不构成新的外部运行或硬件声明；
- E7 使用新 parity 与 v130 派生证据重跑，仍为 `gate_passed=false`、`release_allowed=false`、32 blockers；
  QGC EXE/UI hash 未改变，真实 Jetson/Pixhawk/相机、人工视觉、HIL/REAL、实飞和最终发布仍待现场完成；
- 最终全量回归 3,572 tests：`3570 passed, 2 skipped, 0 failed, 0 errors`，耗时 220.80 秒；JUnit
  520,969 bytes、SHA-256 `2E214E2D7BA697FE531BF4762047CE0BF2AD7278575C2699DF2F02ED2D6CE060`。

## v129 本轮已收口

- 部署器先用 `ssh -G` 解析显式 user/host/port，再从 `KnownHostsFile` 通过 `ssh-keygen -F` 只提取该目标的
  可信 key，写成不可覆盖的每次部署 `.ssh-host-keys` snapshot；scp/ssh 同时指定该 snapshot 并排除 global
  known_hosts，整个远端操作前后再次比较 snapshot SHA，发生变化会删除刚写的 install receipt 并失败；
- installation receipt/Schema 升级 v3，绑定 resolved host、snapshot artifact reference 和排序唯一的 SHA256
  fingerprints。独立 Python verifier 不依赖 ssh-keygen，自行解析 base64 key、重算 fingerprint，并复核明文或
  OpenSSH `|1|salt|HMAC-SHA1` hashed-host token；错主机、snapshot 篡改、重复/伪造 fingerprint、revoked marker、
  未知 key type、注释/空行均 fail-closed；
- 当前真实 `C:\Users\admin\.ssh\known_hosts` 没有 `192.168.144.20`：实际非 package-only 负测在 scp 前以
  `No trusted host key exists` 停止，且 host snapshot、remote output、install receipt 均不存在。负测目录只保留
  dirty-source package/tar 供诊断，不构成远端安装证据；
- 全量回归 3,572 tests：`3570 passed, 2 skipped, 0 failed, 0 errors`，耗时 258.54 秒；JUnit
  520,970 bytes、SHA-256 `FB3D32C97F9A1D7E95DD0FC7B02C85F610042AA063D0C19221E89F2EBA955C0E`；
  Ruff、compileall、PowerShell parse、Git Bash syntax、Windows OpenSSH option smoke、diff/parity 通过；共享
  哈希/QGC EXE 未改变，真实 Jetson/hardware/HIL/REAL/release 未通过，E7 仍有 32 blockers。

## v128 本轮已收口

- `scripts/deploy_jetson_evidence_capture.ps1` 新增显式 `RemoteUser`（默认 `jetson`）和 `SshPort`（默认 `22`）；
  scp/ssh 使用同一精确 `user@host` 与端口，修复直接传 IP 时 OpenSSH 误用 Windows 本机用户名的问题；
- 两段 OpenSSH 调用均强制 BatchMode、`StrictHostKeyChecking=yes`、`UpdateHostKeys=no`，因此未预置可信
  host key、身份变化或需要密码交互时直接失败，不会静默接受或改写主机密钥；任何凭据均不进入源码/回执；
- installation receipt/Schema 升级为 v2，新增 `remote_user` 与 `ssh_port` 严格绑定；独立 verifier 拒绝非法用户名、
  布尔/越界端口。真实 PowerShell `-PackageOnly` 开发烟测生成 6,905,344-byte tar，SHA-256
  `6AF0FE9EDED3B9132C75945718670A77E79C283A8953FA9A28B8D1721A7174A6`，独立复核 242 members、231 files，
  并实证 `INSTALL_RECEIPT_EXISTS=False`；该包 `source_dirty=true`，仅作开发烟测，不是正式部署证据；
- 全量回归 3,566 tests：`3564 passed, 2 skipped, 0 failed, 0 errors`，耗时 271.65 秒；JUnit
  520,094 bytes、SHA-256 `8B5B93EF1F70641AA16100E35B81BD7484CC968AEA9278F401EA891563B8A905`；
  Ruff、compileall、PowerShell parse、Git Bash syntax、diff check 与 parity 通过；五组共享哈希/QGC EXE 未改变，
  真实 Jetson 安装、hardware、HIL/REAL、外场和 release 仍未通过，E7 仍有 32 blockers。

## v127 本轮已收口

- `scripts/deploy_jetson_evidence_capture.ps1` 现在保存完整 SSH 输出；仅在 SSH 退出码为 0、唯一
  `JETSON_EVIDENCE_TOOLS_READY=<exact target>` 与唯一 `JETSON_EVIDENCE_RUN=` 同时存在后，才原子写入安装回执；
- installation receipt 绑定 package receipt bytes/SHA、archive SHA、source Git HEAD、deployment ID、host、远端
  目标、runtime Python、remote output bytes/SHA 和六项远端检查。SSH 失败只保留
  `failed-remote-output.txt`，`-PackageOnly` 不产生安装回执；
- 新增 `multidetect-jetson-evidence-install-verify`、clean-checkout wrapper 和 closed Schema；独立 verifier 会重新
  打开 package receipt/tar 与远端输出，拒绝 package-only 冒充安装、身份漂移、输出篡改、marker 重复/缺失、
  检查失败，以及 sudo/service restart/hardware/release 越界声明；
- 全量回归 3,563 tests：`3561 passed, 2 skipped, 0 failed, 0 errors`，耗时 269.32 秒；JUnit
  519,572 bytes、SHA-256 `A86DAFAE06F727CD2C62AC3ADC5E188E5C7C77EBDA19CF628584DD5F23301EC7`；
  Ruff、compileall、PowerShell parse、Git Bash syntax、diff check 与 parity 重生成通过。当前控制机仍无飞机网段，
  因而没有伪造真实 install receipt；remote install、hardware、HIL/REAL 和 release 仍未通过，E7 仍有 32 blockers。

## v126 本轮已收口

- 新增 `multidetect-jetson-evidence-package-verify`、clean-checkout wrapper、Python strict verifier 与 closed JSON
  Schema；接收方不依赖 PowerShell 生成器的自报结果，直接重开 package receipt 和相邻 tar；
- verifier 流式复核 archive bytes/SHA、source manifest SHA、全部登记源文件 SHA 和闭合文件集合，拒绝绝对/
  `..` 路径、重复/大小写冲突、symlink、hardlink、device/special entry、额外/缺失文件和安全声明提升；
- 正式 v125 包独立通过：240 tar members、229 个受保护源文件、6,668,784 源字节，路径/闭合集合/全部 hash
  均为 true；仍明确 `remote_install_verified=false`、hardware/release false；
- 全量回归 3,552 tests：`3550 passed, 2 skipped, 0 failed, 0 errors`，耗时 260.93 秒；JUnit
  518,004 bytes、SHA-256 `6872C45E81708A11619387F10F2446DE31EC6FE1A968ED796814D2DA741B0155`；
  独立正式包验证报告 735 bytes、SHA-256 `D629E3770F4FB3581C73A78B13BCA46ABD02C923F311F706DE141ABB41F98239`；
  Ruff、compileall、diff check 与 parity 重生成通过，QGC portable 未改变。

## v125 本轮已收口

- 新增 `scripts/deploy_jetson_evidence_capture.ps1`：打包完整 `src/multidetect`、产品版本和 v124 recorder，
  保存 Git HEAD/dirty、逐文件 source manifest、archive bytes/SHA-256 和不可发布声明；
- 生成后先列举安全路径、解包到临时目录、229/229 重算 SHA 并检查闭合文件集，只有
  `package_self_verified=true` 才允许上传；远端再执行相同 manifest 校验、compileall、Bash syntax 与模块 import；
- 远端落位采用独立 `/home/jetson/Multi-Detect-evidence-tools/<deployment-id>` staging→atomic move，已存在版本
  拒绝覆盖；无 sudo、无 systemctl、无服务重启，`-PackageOnly` 可在断开飞机网段时生成可审计包；
- 干净 `14b1010...` 上正式包 6,880,256 bytes、SHA-256
  `6D2D557726ED0EF213CD8411DC83F399C14888C3A5F311BE0AACAA7933F2F49E`，229/229 自复核；
  当前控制机源地址仍为 `192.168.1.13`，不在飞机 `192.168.144.0/24`，所以未声称真实 Jetson 安装；
- 全量回归 3,545 tests：`3543 passed, 2 skipped, 0 failed, 0 errors`，耗时 273.15 秒；JUnit
  517,008 bytes、SHA-256 `2C60726D0F2AC2EB14D3A1BE7C5889CDB6B300CB386E7CB4855BA0AE67DEA096`；
  Ruff、compileall、PowerShell parse、Git Bash syntax、diff check 和 parity 重生成通过，QGC portable 未改变。

## v124 本轮已收口

- `scripts/record_jetson_rtsp_evidence.sh` 从单独录像升级为完整现场证据会话：每次创建独立 timestamp/session root，
  依次执行 H.265 RTSP stream-copy、v3 严格 manifest、camera-only raw replay 转换和 v123 transfer inventory；
- source URI 仍只从环境变量读取；会话根已存在时拒绝覆盖，任何阶段失败都保留部分原始证据并打印恢复位置；
  成功只输出 session root 与 inventory 路径，不启动 detector、QGC、飞控或载荷控制；
- 支持 `SESSION_ROOT`、`FRAME_STRIDE`、可选 `FRAME_LIMIT`；真实 Git Bash `bash -n` 与组合专项测试通过；
- 全量回归 3,544 tests：`3542 passed, 2 skipped, 0 failed, 0 errors`，耗时 275.09 秒；JUnit
  516,859 bytes、SHA-256 `2831EB324A468072DDE72170AF742B24BF3D7D9B58EF15B04F1482CA6B57AE16`；
  全仓 Ruff、compileall、diff check 和 parity 重生成通过，parity/QGC portable 均未改变。

## v123 本轮已收口

- 新增 `multidetect-evidence-transfer` 和 clean-checkout wrapper：`snapshot` 在 Jetson 侧生成不可覆盖的 closed
  inventory，逐文件绑定 portable relative path、bytes、SHA-256、总数、总字节与 canonical bundle hash；
- `verify` 在 Windows 复制目录上重新枚举完整文件集并逐项比较，拒绝缺失、增项、内容/大小变化、symlink、
  大小写冲突和把验证回执写入被验证目录；验证回执独立保存源 inventory 身份与目的端完整清单；
- inventory 与 verification 各有 strict verifier 和 JSON Schema；两者固定 `hardware_acceptance=false`、
  `release_ready=false`，目录改名不影响内容验证，仍不替代真实设备、人员或外场验收；
- 全量回归 3,544 tests：`3542 passed, 2 skipped, 0 failed, 0 errors`，耗时 260.53 秒；JUnit
  516,859 bytes、SHA-256 `5593142E374B2A05F90A2EA1EC3DD17EFBAC15BAC39355C84514537B740C3281`；
  全仓 Ruff、compileall、diff check 与 parity 重生成通过，parity 字节未变化，QGC portable 未重建。

## v122 本轮已收口

- RTSP v3 manifest 在视频位于清单目录内时写规范相对引用；清单+容器从 Jetson 复制到 Windows 后无需改 JSON，
  原 canonical hash、container bytes/SHA 与 full probe 仍可重算；目录外视频才保留绝对引用；
- raw replay 的 source evidence binding 为 manifest/video 各写 `path_base=recording_root|absolute`；同盘时优先
  相对 raw root，使“RTSP 清单+容器+raw replay”整个父目录可搬迁，跨盘无法相对化时明确退回绝对绑定；
- verifier 以搬迁后的 raw root 解析权威引用；summary 里的旧显示路径只作历史信息，raw manifest 固定从当前 root
  打开，所有 hash/bytes/canonical/frame checks 保持。测试实际移动完整目录后再次通过全链复验，并拒绝 summary 篡改；
- 全量回归仍为 3,537 tests：`3536 passed, 1 skipped, 0 failed, 0 errors`，耗时 258.60 秒；JUnit
  515,775 bytes、SHA-256 `EA5F7A260DAE01DED9F28CBEFC2E534EB7C74E99027CDFECE5FDE8CA110112AF`；
  全仓 Ruff、compileall 和 parity 重生成通过，五组共享契约与 QGC portable 未改变，E7 仍为 32 blockers。

## v121 本轮已收口

- `record-rtsp-evidence` 清单由 v2 升级为 closed-schema v3：采集调用前后取真实 UTC、保留单调时间窗，
  将视频 bytes/SHA-256、全帧媒体 probe、stream-copy/EOS/无控制声明纳入 canonical hash；输出路径规范为绝对路径，
  URI 和凭据仍不写入文件；
- 新增 `multidetect-rtsp-to-replay`、`src/multidetect/rtsp_evidence_replay.py` 和 clean-checkout wrapper；命令先
  严格重开 v3 清单及原视频，再把清单的 capture start 自动作为 raw replay UTC 起点，输出目录已有时拒绝覆盖；
- video converter 在解码前后各计算一次源 bytes/SHA-256，拒绝证据不符或转换途中改变；summary 绑定 source
  manifest/file/capture window 并带 canonical hash，转换完成后 verifier 再重开 RTSP source、summary、raw manifest、
  event stream 和全部 PNG；
- tracking-review 对 v2 保持历史兼容，新 v3 必须通过同一严格 verifier。真实 OpenCV 容器端到端及篡改负例已覆盖。
  camera-only 输出仍固定未知 GPS/位置、未证明人员/设备/地点、`release_ready=false`；E7 32 blockers 未改变。
- 全量回归 3,537 tests：`3536 passed, 1 skipped, 0 failed, 0 errors`，耗时 254.10 秒；JUnit
  515,775 bytes、SHA-256 `BD7D0B2718E1584685019DCD7D8BFA807DDA16AD239481F5252651681385E1AD`；
  全仓 Ruff、compileall、schema parse 与 parity 重生成通过，parity 字节未变化。

## v120 本轮已收口

- `create_video_replay_recording` 的 summary/`recording-summary.json` 新增 `source_attestation_handoff`：从重新
  打开的 raw manifest 派生准确 start、minimum end、latest event，列出仍须操作员确认的五项 provenance；
- `multidetect-deployment-recording-attestation` / wrapper 新增 `--derive-times-from-recording`，避免手抄 UTC；
  该开关不推断现场真实性，stratum、captured-by、device、site、local/RTSP 仍全部必填；
- 自动时间和显式时间互斥；video converter 的 `1970-01-01T00:00:00Z` camera-only 默认锚会被标记
  `attestation_time_eligible=false`，attestation writer/verifier 均拒绝把它升级为物理现场来源；
- 专项回归覆盖纯派生不落证明、显式人员字段要求、RTSP 证明、占位时间拒绝和 video→GNSS join 不回退。
  真实现场采集、人员审核、HIL/REAL、E7 32 blockers 与 QGC portable 均未改变。
- 全量回归 3,530 tests：`3529 passed, 1 skipped, 0 failed, 0 errors`，耗时 271.99 秒；JUnit
  514,802 bytes、SHA-256 `79EBC8B57C6471AFC2EFE3458258A34489F8E1BAEDD2EE2D4726A6964B8B82C0`；
  全仓 Ruff、compileall 和 diff check 通过。

## v119 本轮已收口

- 新增 `multidetect-deployment-recording-attestation` 和 clean-checkout wrapper；每个物理现场 raw recording
  必须写入不可覆盖的 `deployment-recording-source.json`，记录 canonical stratum、采集人、设备、地点、
  local/RTSP 来源与 UTC 起止，并绑定 recording manifest 的 bytes/SHA-256/recording hash；
- verifier 要求起点与 recording 一致、终点覆盖最后 frame/telemetry，重开 event/image 全链；closed schema、
  strict JSON、canonical hash、非 symlink 和 write-new 语义拒绝模糊或事后覆盖；
- deployment intake 把证明加入逐文件 inventory 与 receipt 摘要，field campaign 缺任一证明会停在
  `awaiting_source_attestations`，不会进入标注；
- `ReplaySetManifest` 新增 `source_attestation` artifact reference；builder/loader 强制 deployment 条目绑定
  同目录、同 stratum、同 recording 的证明并重新哈希，直接自报 source kind、旧无证明清单、移动或篡改均失败；
- lab/synthetic 历史清单继续兼容。证明是完整性绑定的人为来源声明，不是硬件签名、可信时间戳或独立外场
  验收；真实六分层采集、标注、模型评审和 E7 32 blockers 均未改变，共享 parity/QGC EXE 不重建。
- 全量回归 3,527 tests：`3526 passed, 1 skipped, 0 failed, 0 errors`，耗时 269.24 秒；JUnit
  514,344 bytes、SHA-256 `FF3FF9F0F0A8944A4EF6DA883D7F50393206C3C325A30716655590C9303B3883`；
  全仓 Ruff、compileall、schema parse、release parity 重生成与文档门通过，parity 文件逐字节未变化。

## v118 本轮已收口

- `multidetect-field-replay-campaign` / `scripts/run_field_replay_campaign.py` 新增 `annotation-ui`：启动前
  fresh 重开不可变 plan、模型哈希、intake receipt 和 annotation workspace，只有 authoritative stage
  为 `annotation_required` 才把当前固定路径交给 v117 工作台；
- campaign status 新增 `annotation_workbench_argv`，可以直接复制运行；尚未完成 intake/workspace、已完成
  review、证据漂移或其他 stage 都不能借该入口修改标注；
- stdout 先输出 `field_replay_annotation_workbench_authorized`，绑定 campaign ID、dataset ID/version、
  plan canonical hash 和 stage，再由工作台输出随机 loopback URL、`loopback_only=true` 与 `hardware_io=false`；
- 回归覆盖参数解析、默认 UI 命令、intake-ready 与 annotation-finalize-ready 拒绝，以及真实子进程随机端口
  启动、HTTP 页面读取和终止；这仍不代替真实人员视觉/交互、标注或复核；
- 全量回归 3,521 tests：`3520 passed, 1 skipped, 0 failed, 0 errors`，耗时 220.230 秒；JUnit
  513,503 bytes、SHA-256 `FCA8182DF1650B2FB2CAB2D4F076DF52903155369542116B06BCA019B6DA848A`；
  全仓 Ruff 通过。共享 parity、QGC C++/QML、portable EXE 与 E7 32 blockers 均未改变。

## v117 本轮已收口

- 新增 `multidetect-replay-annotation-ui` 和 `scripts/run_replay_annotation_workbench.py`：本机浏览
  已验证 intake 图像，在 canvas 上拖拽多类别 bbox，逐帧保存 positive/negative，支持前后导航、状态计数、
  清除未保存框和独立 reviewer 提交；底层复用 v116 原子 annotation/review API；
- HTTP 服务仅允许数值 loopback 地址，默认 `127.0.0.1:8765`；随机 mutation token、CSP nonce、无 CORS、
  256 KiB 请求上限、严格 JSON（重复键/非有限数/额外字段拒绝）共同保护 mutation；
- 每次页面读取图像都会重新验证 intake/workspace、frame ID、相对路径、目录 containment、非 symlink、字节数
  与 SHA-256；服务启动事件固定 `loopback_only=true`、`hardware_io=false`，不访问相机、飞控或执行器；
- HTTP 生命周期回归覆盖页面/CSP、真 PNG、无 token、重复键、额外字段、正例、五个负例与最终 review binding；
  内嵌 JavaScript 已用 Node `--check` 验证。真实人工视觉/交互验收仍待现场执行；
- 全量回归 3,519 tests：`3518 passed, 1 skipped, 0 failed, 0 errors`，耗时 265.846 秒；JUnit
  513,238 bytes、SHA-256 `22AB545FB5BEB86FBB54766ACEE1CB806CEB8361ECE57EF59D59227A42E811FA`；
  全仓 Ruff 通过。共享 parity、QGC C++/QML、portable EXE 和当前 E7 32 blockers 均未改变。

## v116 本轮已收口

- 标注安装入口和 clean-checkout wrapper 新增 `annotate`、`review` 命令：frame ID 精确寻址，positive
  接受一个或多个 `LABEL:X1:Y1:X2:Y2`，negative 明确无目标，全部修改均做 intake/assignment 复验、
  annotator 锁定、并发内容检查和同目录原子替换；
- inspect 新增下一未标帧的 stratum/frame/image 身份；campaign 的 `next_argv` 直接指向该帧，全部标完后
  切换成 review 命令，不再要求人工裸改 reviewer 字段；
- workspace schema v2 新增 `review_content_hash`，绑定 assignment、全帧 annotation、annotator、reviewer
  和 UTC timestamp；复核后实际改标会撤销 review，文件外篡改或 reviewer 换人会被 fresh inspect 拒绝；
- v1 reviewed workspace 继续保持历史可读/finalize；新 campaign 强制 v2 + review binding，拒绝把 v2 降级成
  无内容绑定的 v1 后继续自动流水线；
- 全量回归 3,518 tests：`3517 passed, 1 skipped, 0 failed, 0 errors`，耗时 235.59 秒；JUnit
  513,092 bytes、SHA-256 `D8ECBFD450D0B168F22533CA57D72BF562675117B091318D1E34EADA2948B7DC`；
  全仓 Ruff 通过。共享 parity、QGC C++/QML、portable EXE 和当前 E7 32 blockers 均未改变。

## v115 本轮已收口

- 新增不可覆盖的 `field-replay-campaign.json` 计划和 `create/status/advance` CLI，把六个真实来源目录、
  固定模型/manifest、推理参数、accuracy 门限和 calibration grid 全部做 path/hash/bytes/canonical 绑定；
- 自动推进 intake、annotation workspace、finalize、正式 ONNX runtime predictions、reviewed accuracy、
  reviewed threshold calibration 和独立 fresh verification；真实记录缺失或人工标注/复核缺失时停在明确 stage，
  不生成正向 claim；
- annotator、annotation reviewer、accuracy reviewer、threshold reviewer 强制四人互异；重新打开 campaign
  会逐层复验 receipt/report/model，手工替换或篡改继续 fail-closed；
- synthetic HIL ONNX 六分层端到端测试已实际走到 `complete`，并覆盖 pending、模型篡改、CLI 和幂等恢复。
  这只证明编排软件可运行，现场六分层采集和人工审核仍未执行；
- 全量回归 3,515 tests：`3514 passed, 1 skipped, 0 failed, 0 errors`，耗时 208.81 秒；JUnit
  512,680 bytes、SHA-256 `7484ECD43DA22A8B2E903DF539EFDDEEAFDAECAEB65673658DC568BA227E0D50`；
  全仓 Ruff 通过。共享 parity、QGC C++/QML 和 portable EXE 未变，当前 E7 仍为 32 blockers。

## v114 本轮已收口

- E7 calibration verification 结果新增聚合 `promotion_ready`：只有 report 与当前输入重新计算都明确为 true
  才成立；历史缺失、任一 pending 或不一致都不能获得该状态；
- prerelease 仍允许读取和绑定 `promotion_ready=false` 的确定性诊断，不破坏阈值探索和历史证据；
- final release 在 v113 provenance 门之外进一步要求 promotion-ready，封死“完整性通过但阈值未独立批准”仍能
  进入最终版的缺口；专项测试实际构造未 reviewed calibration，证明 prerelease 可读而 release 明确阻断；
- 当前 parity 未变化；E7 重算新增诊断字段后仍为 `gate_passed=false`、`release_allowed=false`、32 blockers，
  calibration 的 `promotion_ready=false`、`provenance_complete=false` 与现有 SIM fixture 一致；
- 全量回归 3,512 tests：`3511 passed, 1 skipped, 0 failed, 0 errors`，耗时 197.330 秒；JUnit
  512,248 bytes、SHA-256 `C4B5020705A7501B433EABE7DBA80DB9DACBE696DD5DA3D0A6BD4DA159460795`。

## v113 本轮已收口

- calibration verifier 新增 `report_provenance_complete` 与 `recomputed_provenance_complete`，分别记录文件声明
  与按当前输入重新计算后的 provenance；
- E7 writer 将两项布尔值与 verification artifact、calibration report、replay manifest 的 raw/canonical hash
  一起绑定，evaluator 每次重新打开并复算后输出汇总 `provenance_complete`；
- v111 及更早的报告/verification/E7 绑定缺少新字段时仍可在 prerelease 读取，统一投影为 false，不伪造来源；
- 最终 release channel 现在要求 calibration verification 存在、通过且 provenance 完整；该要求不替代独立
  accuracy evidence，也不让 synthetic HIL 模型获得生产批准；
- synthetic HIL ONNX 测试贯通 reviewed truth → runtime prediction receipt → reviewed accuracy → calibration → E7，
  并覆盖旧 artifact 兼容、正式版阻断与篡改拒绝；共享 parity 因 release-gate schema 改动已重算。
- 从既有不可变 runtime/AI、三机型正向与故障源重算 external join/revalidation、PX4 matrix/campaign 后，
  当前 parity 的 E7 wrapper 完整执行，仍为 `gate_passed=false`、`release_allowed=false`、32 blockers；
- QGC C++/QML 与 UI parity 未变，EXE 不嵌入 E7 Schema/parity 文件，因此不重复构建 portable；旧机器证据保留，
  新派生证据只重绑当前共享契约，不宣称重新执行 Gazebo、硬件或飞行。
- 全量回归 3,511 tests：`3510 passed, 1 skipped, 0 failed, 0 errors`，耗时 192.488 秒；JUnit
  512,114 bytes、SHA-256 `20660335E255D76787B9797E2EBF6219392FE6C161F76C61284B53066DF4DC83`。

## v112 本轮已收口

- 将 v110 prediction receipt 验证抽成 evaluator/calibration 共用函数；每次都会重开 annotation、raw、truth、
  model/manifest、六分层 predictions、replay manifest 和 receipt，并返回同一套 model/run binding；
- prediction binding 新增 `production_approved`，与 runtime inference 和 synthetic HIL 分开记录；候选或 synthetic
  模型不会因为完成推理而被描述为生产批准；
- calibration API、wrapper、安装主 CLI 新增成对的 `--prediction-run-receipt` 与 `--accuracy-report`；只给一个会
  fail-closed，完整输入会先执行 readiness fresh recomputation，再核对 accuracy 的 model/run binding；
- calibration report 新增 prediction receipt、accuracy 文件、manifest/canonical、model artifact 等哈希绑定及
  `provenance_complete`；旧报告仍可重算，但该字段为 false，保持 prerelease 历史兼容；
- verifier 可从报告恢复两条路径，也支持复制工作区时显式 override；替换一个仍能独立自洽的 accuracy report
  也会使 calibration deterministic recomputation 不同而失败；
- synthetic HIL ONNX 端到端测试实际完成 reviewed 六分层 truth、runtime predictions、reviewed accuracy、
  calibration、schema validation、CLI parity、独立 verify 和 tamper rejection；
- 全量回归 3,510 tests：`3509 passed, 1 skipped, 0 failed, 0 errors`，耗时 214.725 秒；JUnit
  511,970 bytes、SHA-256 `E9AD6C579FD88B3953195EB9207FAB775EAED6B2E3BDCD997600AB334B8D3B49`；
  全仓 Ruff、compileall、schema parse 与 diff check 通过。

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
| `field-link-preflight-live-20260907-v155-current.json` | 结构复验通过；`hardware_link_ready=false` | 3/3 Jetson/GR01/RTSP 超时；三条路由源均为 `192.168.1.13`，不在飞机网段 | `740479B51DA65F0564C5436FEF9CA6C006775375AA5A20642A484A7988AEC8D7` |
| `simulator-external-runtime-ai-join-v155-current-parity-pre8.json` | `gate_passed=true` | 三机型历史外部 source 按 v155 parity 重算 | `C4F7EEECD3EB2AD1FB5CA693DF09826876E480B2AB4A069DCDB15BCC15157E29` |
| `px4-reported-position-response-matrix-v155-current-parity-pre8.json` | `gate_passed=true` | fixed-wing/multirotor/VTOL `GLOBAL_POSITION_INT` response | `7693C40A84D39339E73CA28F49E0596C13E199A5EF01115534654F9AC900AEC9` |
| `px4-reported-position-response-campaign-v155-current-parity-pre8.json` | `gate_passed=true` | 6 正例、9 个 fail-closed 故障 | `112EF4627A2CB1F66953EB30BD3D85DD4EB52AD0E816AAF8A6B074BF82E998E2` |
| `release-gate-pre.8-current-20260906T192400Z-v6-bound-report.json` | `gate_passed=false` / `release_allowed=false` | 31 blockers | `996F595DE8E07EB41F63A1EDB887637DC8ABB0B5ACD6A5197D597C549E93CCEF` |
| `field-link-preflight-live-v156-current.json` | 结构复验通过；`hardware_link_ready=false` | 3/3 Jetson/GR01/RTSP 超时；路由源仍为 `192.168.1.13` | `FE782E46396A28E8E15DCF6983B2C1C36EEA4C28F91F393809BBA88FEDE87B57` |
| `simulator-external-runtime-ai-join-v156-current-parity-pre8.json` | `gate_passed=true` | 三机型历史外部 source 按 v156 parity 重算 | `6DA1C013A78616B3FCE433145E8F1A86B785F8E7AF8F6443430DF05A0A2E0743` |
| `px4-reported-position-response-matrix-v156-current-parity-pre8.json` | `gate_passed=true` | fixed-wing/multirotor/VTOL `GLOBAL_POSITION_INT` response | `9A4AA992E64735E0401B5768D4E6759C24001AD8A017C77A66CCA85DB2518610` |
| `px4-reported-position-response-campaign-v156-current-parity-pre8.json` | `gate_passed=true` | 6 正例、9 个 fail-closed 故障 | `FE96FC6051570BEF85270D538A650CD9B61EBB2B72515CBE1563EAC7CFD5CAE9` |
| `release-gate-pre.8-current-20260906T201700Z-v6-bound-report.json` | `gate_passed=false` / `release_allowed=false` | 31 blockers；当前 v156 parity | `2E69421F6446D6064B692AD0580B43C5D9CF3BCEA3AF8944C918CB2CA1D0C543` |
| `field-link-preflight-live-v157-current.json` | 结构复验通过；`hardware_link_ready=false` | 3/3 Jetson/GR01/RTSP 超时；路由源仍为 `192.168.1.13` | `49C25699A416A266C83362798049F9C8FEAF3D9A0C9BF57A8EE364D962DC053B` |
| `simulator-external-runtime-ai-join-v157-current-parity-pre8.json` | `gate_passed=true` | 三机型历史外部 source 按 v157 parity 重算 | `E293377814A455F03841110D7F51349775CEFDFACFACF83FF30A0D5BA76EDC7B` |
| `px4-reported-position-response-matrix-v157-current-parity-pre8.json` | `gate_passed=true` | fixed-wing/multirotor/VTOL `GLOBAL_POSITION_INT` response | `0670435A201876DD75A0DCF51E3CF0C29E8BFD65091F929B614C6EA02E8F7CCE` |
| `px4-reported-position-response-campaign-v157-current-parity-pre8.json` | `gate_passed=true` | 6 正例、9 个 fail-closed 故障 | `334C7C4131740C8869F97459F43F6BC93FE2C8C03B636A092078A8ED584E84F2` |
| `release-gate-pre.8-current-20260906T202947Z-v6-bound-report.json` | `gate_passed=false` / `release_allowed=false` | 31 blockers；当前 v157 parity | `AB4E9ADEC704735281F6ACA7CDB99824AA586F0ABA98C77020984EBFC033E3E3` |
| `field-link-preflight-live-v158-current.json` | 结构复验通过；`hardware_link_ready=false` | 3/3 Jetson/GR01/RTSP 超时；路由源仍为 `192.168.1.13` | `E23786D4725F2B1923D69FA5A138B19E18AD495A776ADD1D2AA1C8D905E12B3A` |
| `simulator-external-runtime-ai-join-v158-current-parity-pre8.json` | `gate_passed=true` | 三机型历史外部 source 按 v158 parity 重算 | `6882A353E480CA0CAA9982044C8FBAC4404489AA149BCE853F87EA681A43E652` |
| `px4-reported-position-response-matrix-v158-current-parity-pre8.json` | `gate_passed=true` | fixed-wing/multirotor/VTOL `GLOBAL_POSITION_INT` response | `AC9CF24B004F6F8EE9517B49EEAF3EB4CD3D2BA8864FDA729705850FD5806EE7` |
| `px4-reported-position-response-campaign-v158-current-parity-pre8.json` | `gate_passed=true` | 6 正例、9 个 fail-closed 故障 | `AD0B3D53B80FCED8683453A249DEB746C9E4E6EC6D5062EFE0890D2757732E76` |
| `release-gate-pre.8-current-20260906T205843Z-v6-bound-report.json` | `gate_passed=false` / `release_allowed=false` | 31 blockers；当前 v158 parity | `55644F69519C1B17C55F1AC446C11EDBA1F63D86C0D8871802E57B2B931F7E4E` |

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

`configs/release-parity.json`：8,735 bytes，文件 SHA-256
`289FD7BD12E12B3931D5AEDD3B5C94A34EF5559480F338B6AA139C9D07184CFF`。

| 契约组 | 当前哈希 |
|---|---|
| UI contract | `3ba8008a73ed19bd9c5b774a322989a841263b947a395c977a660c55176bebd7` |
| Core algorithm | `1c9f173385cd21975096ed67991228d04ecb10c1277d4634420d3a4da6865e3e` |
| Task contract | `2cdb5814c09dfad6609b60c71ae9affe1ad5ffcee63f5e4964590799f34c254b` |
| Safety policy | `a142109e93584194cf9a276cd6c430105d03baba3c3114cec44a19a532955fcc` |
| PX4 Mission path | `df1cab8dd37c203cd8a86c6cee714cdc8650c3cd626376b2584d322ef2b6f4bf` |

## 功能成熟度矩阵

| 子系统 | 已实现/已自动验证 | 当前有效边界 | 仍需完成 |
|---|---|---|---|
| 输入与运行模式 | 本地/RTSP/Replay/Gazebo；REPLAY/SIM/HIL/REAL profile 与五组 parity | 软件契约和模拟输入 | 四模式同屏人工验收与真实 profile 证据 |
| 感知与模型治理 | ONNX provider、manifest、raw/post-NMS、候选隔离、延迟/精度工具 | v56 COCO car 仅 quarantined SIM candidate | 部署域 fire/smoke/safety 模型、数据集、评审和阈值 |
| 多目标跟踪 | 多目标池、短时恢复、ReID、遮挡/丢失/重获、TRK/LCK/TGT | 自动化与软件/SITL | 真实复杂场景身份精度与 G20/QGC 触控验收 |
| 测距 | metric depth、相机地面射线、多源融合、DEM/GNSS、时间/运动/残差不确定性 | v59 实际 Gazebo 像素；三档坡高/距离；advisory-only | 真实外参与 outdoor survey |
| 目标落图 | WGS-84、1σ 圆、GPS/RANGE fail-closed、v6 GNSS-bound 本地证据与 epoch 诊断 JSON | v1-v5 只读兼容；v6 保存原子观测健康审计；三正场景 15/15 落图、近地平线 0/5 假点 | 人工视觉签字与真实 GNSS |
| MultiDetectGCS | 原生 Fly View 覆盖、状态、目标地图、operator protocol、portable build | v160 portable/simple boot/五项 native self-test 通过；原生 epoch 弹窗与原子 JSON 保存 | 四模式人工视觉对等与 E7 绑定 |
| 数字孪生 | PX4/Gazebo、三机型、场景/地图/DEM、wire v3 covariance、故障、watchdog | 三机型外部 wire-v3 实采；x500 position response；三档定位 | estimator 非零 covariance；fixed-wing/VTOL 对等动态响应 |
| 任务与控制 | 平台无关 planner、PX4 Mission 编译/反馈、Mode 3 动态权限 | 测试/SIM/HIL 框架；物理输出默认锁定 | 六份 HIL/REAL×三机型 feedback 与真实飞控验收 |
| Jetson/Pixhawk/链路 | Jetson profile、RTSP bench、只读 MAVLink、V6X 参数备份、G20/GR01 认证协议 | 软件/localhost 与部分只读现场快照 | 实机 Jetson provider、相机、GR01、V6X 同时在线验收 |
| 载荷安全 | 独立授权、双反馈、惰性 HIL、超时/重放/状态不一致 fail-closed | 无物理输出的 HIL | 经评审真实 controller transport 与受控地面台架 |
| 发布门 | E7 schema、证据绑定、recompute、严格/报告模式 | 当前 31 blocker，发布拒绝 | 全部现场/模型/视觉/Mission/HIL/REAL 门关闭后才发 v0.3.0 |

## 本机可运行 QGC

当前 portable 主程序：

```text
D:\Projects\Multi-Detect\build\windows-v0.3.0-pre.8-release\portable-v0.3.0-pre.8\bin\MultiDetectGCS.exe
```

- 大小：`46,875,136` bytes
- SHA-256：`41EA7AAF830B406AADBCBB17BD7E5E1E22C1A08F20DCBA002885E0153CF6765B`
- 当前 portable 已包含 v175 的全量 replay 摘要与一键复制、v174 质量统计、v172 媒体/车辆明细、
  producer epoch 诊断、evidence v6 与全部此前 QGC C++/QML 变更；native self-test 和 offscreen simple boot 均通过。
- 该文件位于被忽略的本机 `build/`，不是 GitHub 下载资产；运行时必须保留完整 portable 目录。

## 当前 E7 阻断分类

当前 31 条 blocker 可归为：

1. replay-set 没有完整部署域覆盖、精度批准和人工评审；
2. REAL field-link 报告新鲜但 `hardware_link_ready=false`：当前接口不在飞机网段，Jetson/GR01/相机均超时；
3. REPLAY、SIM、HIL、REAL 的人工 QGC visual 和 Mission path acceptance 未全部通过；
4. HIL/REAL 缺真实 hardware acceptance 与 real-control acceptance；
5. PX4 Mission feedback 六叶矩阵未完成；
6. 部分被引用 artifact 自身仍为失败门；
7. 产品仍是 `prerelease`，版本面不能伪装为最终 `0.3.0`。

## 下一步优先级

### 仓库维护：Ruff format 基线已迁移

v130 已用独立机械提交完成 1,523 个 Python 文件的 format baseline，并重建 parity、三机型派生证据和 E7。
剩余验证仅是 GitHub-hosted Python 3.11/3.12/3.13 manual workflow 实际运行，不再存在本地 format backlog。

### P0：原生 QGC 目标落图与证据卡片人工签字

v61 已将 high-slope evidence 的坐标、1σ、类别、距离和方位经签名协议持续送入原生
MultiDetectGCS，并完成 exact PID/HWND 动态地图/证据卡片机器采集。下一步由人工检查同一证据画面并
使用 v64 checklist 生成 accepted record；独立 verifier 通过后再经 manifest 接线评审接入 E7。当前
负向 source-binding 预检保持 5/5 检测、0 假点。v67 combined-SIM source 已能被通用 adapter 识别为
真实 SIM runtime，但它的 toolbar/Mission 截图没有配置 target-geolocation acceptance，不能直接充当
包含地图点/1σ/证据卡片的人工记录；SIM 模式仍需专门运行该定位画面。

### P0.5：把 typed RuntimeObservationResult 接入真实 HIL/REAL producer

v187 已把 result/sidecar 的生产字段收拢到不可变 builder，并验证正向 pair 与 input-hash 篡改负向。
下一步接入 `capture_pixhawk_odometry_covariance.py` 的现场适配层时，必须保留 telemetry request 计数，
另设 application/flight-control message counter；只有后者为零且 RuntimeProfile、Mission feedback、
endpoint/vehicle identity 同版绑定，才能生成 v186 receive-only 结果。不能把 telemetry 请求数抹成零，
也不能用软件 fixture 关闭硬件/HIL/REAL 门。

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
