# Authenticated fire-alert data link

Multi-Detect can send confirmed fire alerts over an IP-capable telemetry link using authenticated
UDP. This channel is one-way application data plus a receiver acknowledgement. It cannot carry
flight commands, mission changes, authorization decisions, or payload-control messages.

## Security and reliability contract

- HMAC-SHA256 authenticates and protects the integrity of both alert and acknowledgement packets.
- Sender ID, receiver ID, protocol version, message type and wall-clock timestamp are signed.
- The signed header uses strict JSON types: `version` is integer `1`; `sent_at_unix_s` is a
  finite integer/float and JSON booleans are rejected rather than being treated as integers.
- Each acknowledgement must match both the alert ID and a fresh 128-bit request nonce.
- Packets outside the configured clock-skew window are rejected.
- The receiver remembers delivered alert IDs. An exact retransmission is acknowledged as a
  duplicate; reuse of an alert ID with different content is rejected. The optional SQLite store
  preserves this decision across receiver restarts. Both the memory and SQLite stores retain a
  configurable, bounded window (10,000 IDs by default) so long-running ground stations do not grow
  without limit; downstream consumers must still use `alert_id` as their own idempotency key.
- The aircraft SQLite outbox records an alert before transmission and marks it delivered only after
  a valid correlated acknowledgement.
- The outbox automatically retains only the latest 10,000 delivered rows in the same transaction
  that marks delivery. Pending or failed rows are never removed by this retention policy; callers
  may choose another non-negative limit or disable automatic pruning explicitly.
- Retries and acknowledgement waits are bounded. Delivery failure does not affect Pixhawk or call
  any payload interface.

HMAC provides authentication and integrity, not confidentiality. Use a private/VPN data network or
an encrypted radio link if aircraft position or imagery-derived metadata is sensitive. The two
computers need a trustworthy time source. Production ground operation should enable the SQLite
deduplication database and the downstream ground application should still treat `alert_id` as its
idempotency key.

## E7 separates alert ACK from GR01 operator ACK

The final release gate treats the fire-alert link and the GR01 target-selection link as separate
evidence domains:

- `real_alert_link` proves authenticated fire-alert delivery and a correlated receiver ACK;
- `real_gr01_operator_link` proves the signed target-selection command/ACK round trip over a real
  non-loopback GR01 path whose sender source address belongs to the declared aircraft CIDR.

Passing one never satisfies the other. The GR01 record is accepted only from a strict
`gr01-link-bench` report and is reverified from its hash-bound source during every E7 evaluation.
Use `-RealGr01OperatorLinkBench <report.json>` with `run_current_release_gate_pre8.ps1`; omitting it
keeps prerelease diagnostics available but leaves final `0.3.0` blocked. Neither record enables
flight control or physical payload release.

## Local loopback HIL

Use a random key with at least 32 bytes. Keep it in the process environment or a mode-0600 service
environment file; never put it in a command argument, source file, log, or issue report.

Ground receiver, PowerShell terminal 1:

```powershell
$env:MULTIDETECT_ALERT_KEY = '<REPLACE-WITH-AT-LEAST-32-RANDOM-BYTES>'
multi-detect alert-udp-receiver `
  --bind-host 127.0.0.1 --port 14600 `
  --hmac-key-env MULTIDETECT_ALERT_KEY `
  --receiver-id ground-station-1 --expected-sender-id aircraft-1 `
  --deduplication-db artifacts/received-fire-alerts.sqlite3 `
  --deduplication-capacity 10000 `
  --max-messages 1 --receive-timeout-seconds 60
```

Aircraft live process, terminal 2:

```powershell
$env:MULTIDETECT_ALERT_KEY = '<SAME-KEY-AS-RECEIVER>'
multi-detect live-camera configs/missions/fire_patrol.demo.json `
  --source 0 `
  --onnx-model models/fire-smoke-nms.onnx `
  --model-manifest models/fire-smoke-nms.manifest.json `
  --class-names fire,smoke --output-coordinates normalized_xyxy `
  --alert-udp-host 127.0.0.1 --alert-udp-port 14600 `
  --alert-hmac-key-env MULTIDETECT_ALERT_KEY `
  --alert-sender-id aircraft-1 --alert-receiver-id ground-station-1 `
  --alert-outbox artifacts/fire-alerts.sqlite3
```

GR01 直连 topology 文件结构如下；`remote_host` 必须替换为现场确认的 G20/地面端 IPv4，不能填写主机名：

```json
{
  "schema_version": 1,
  "topology_id": "aircraft-1-gr01-direct",
  "mode": "gr01_direct",
  "remote_host": "192.168.144.21",
  "remote_port": 14600,
  "expected_source_cidr": "192.168.144.0/24",
  "sender_id": "aircraft-1",
  "receiver_id": "ground-station-1",
  "hardware_id": "GR01-ALERT-REPLACE-SERIAL",
  "fallback_evidence": null,
  "advisory_only": true,
  "flight_control_enabled": false,
  "physical_release_enabled": false,
  "payload_release_requested": false
}
```

### Bounded alert-link bench

现场接通后，用同一个认证 UDP 端点先做有界告警 ACK 压测。它复用 live patrol 的
`UdpAcknowledgedAlertTransport` 和重试策略，每个样本都生成新的 `alert_id`，只把收到匹配
ACK 的样本计入成功；重试次数、缺 ACK 次数、丢包率和 ACK P50/P95 都写进 JSON 报告。该
工具永远把 `flight_control_enabled`、`physical_release_enabled` 和
`payload_release_requested` 固定为 `false`。

```powershell
$env:MULTIDETECT_ALERT_KEY = '<SAME-KEY-AS-RECEIVER>'
python scripts/run_alert_link_bench.py `
  --topology artifacts/field/alert-link-topology.json `
  --hmac-key-env MULTIDETECT_ALERT_KEY `
  --minimum-round-trips 100 --maximum-attempts 3 `
  --maximum-packet-loss-rate 0.01 `
  --maximum-ack-latency-p95-ms 500 `
  --out artifacts/evaluation/alert-link-bench-<DATE>.json
```

`--topology` 是现场操作员对端点、设备编号和选定物理路径的原子声明，并自动进入 hardware mode；解析器会拒绝回环、无法解析
的主机名以及非认证 UDP transport，所以本机回环只能作为 software baseline。v2 会在第一个告警发送前
通过 UDP route selection（不发送数据）记录 `local_source_address`；该地址必须属于
`--expected-source-cidr`。v3 还把该地址作为 `source_host` 固定到每一个真实 UDP sender socket；v4 进一步从
每个成功 ACK receipt 保存实际 socket `getsockname()` 源地址，并要求全部观察都与 route/bind 地址一致。route
source、CIDR 或 transport source 任一不一致，ACK 循环都不会启动且 `transmission_attempts=0`；即使收到 ACK，
缺失实际 sender source observation 也不能通过。v5 还内嵌规范化 topology 与 canonical SHA-256；命令行另给
host/port/source CIDR/双方 ID/设备 ID 时必须与 topology 完全一致。选择 `v6x_fallback` 必须在 topology 同目录下
提供并哈希匹配 GR01 直连失败、PX4 SITL 路由和拆桨 V6X 台架三份证据，不能静默自动回退。报告结构由
[`configs/schemas/alert-link-bench.schema.json`](../configs/schemas/alert-link-bench.schema.json)
约束。即使 bench 通过，也只关闭“认证 ACK/时延/丢包”这一条数据链路子门；GR01 型号、
天线/射频稳定性、断链恢复、时钟同步和完整现场 `HIL/REAL` 仍需独立证据复核。

拿到报告后先做离线复核，再把原始报告和复核输出一起归档：

```powershell
python scripts/verify_alert_link_bench.py `
  artifacts/evaluation/alert-link-bench-<DATE>.json --require-passed
```

复核器使用严格 JSON（拒绝重复 key、`NaN`/`Infinity`），重新计算样本计数、缺 ACK 比例、
延迟阈值、事件后缀和 software/hardware 分域；它不会重新打开 UDP，也不会把报告写回为
通过。

通过的现场报告可以作为 E7 的独立 hash-bound REAL 数据链路证据写入发布清单。创建清单时
显式传入 `--real-alert-link-bench`；装配器会重新运行同一个 verifier，只接受
`alert_link_bench_passed`、具体非回环 IP、设备编号以及认证且关联的 ACK。缺少该记录在
prerelease 仍保持向后兼容，但最终 `v0.3.0` channel 会 fail-closed：

```powershell
python scripts/create_release_gate_manifest.py `
  --out artifacts/evaluation/release-gate-pre.8-alert-link-bound.json `
  --real-alert-link-bench artifacts/evaluation/alert-link-bench-<DATE>.json `
  # 其余 --replay/--sim/--hil/--real 与 parity/status 参数照常传入
```

清单中的 `real_alert_link` 会保存报告路径、SHA-256、字节数、端点、设备编号、样本/阈值
摘要和 verifier checks。每次 `check_release_gate.py` 读取时都会重算源报告与这些字段；改
路径、摘要、端点、认证位或把 software baseline 冒充硬件都会重新关闭 E7。该绑定只证明
告警数据链路，不替代 GNSS、测距、执行器、HIL/REAL 或人工 Fly View 验收。

For a real IP radio, replace only the receiver address and validate packet loss, latency, MTU,
reconnect behavior, time synchronization and key rotation on the selected hardware. Serial-only
radios still require a separately tested framing adapter; do not tunnel this protocol blindly over
an unbounded serial stream.

The local Windows HIL has also been exercised as two independent processes using the synthetic
ONNX camera path. The aircraft processed 105 frames, generated one multi-frame-confirmed alert,
received one authenticated ACK and marked its SQLite outbox row `delivered`; the ground receiver
stored the same alert ID and payload hash with no rejection or duplicate. This proves the software
boundary, not GR01 radio performance.

`artifacts/evaluation/terminal-continuation-v28d/alert-link-software-baseline.json` records a
20-round localhost UDP baseline under the strict header type checks (20/20 ACKs, correlated/HMAC
verified, packet loss 0). It is explicitly `simulation_only=true` and is a software regression
artifact, not GR01 or REAL field evidence.

The automated integration suite also covers a ground outage: the first live run exhausts its
bounded attempt and leaves the original alert pending; after the receiver starts, the next live run
retransmits that same alert before reading new frames and marks it delivered only after the signed
ACK. No new alert ID is substituted during recovery.
