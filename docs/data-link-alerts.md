# Authenticated fire-alert data link

Multi-Detect can send confirmed fire alerts over an IP-capable telemetry link using authenticated
UDP. This channel is one-way application data plus a receiver acknowledgement. It cannot carry
flight commands, mission changes, authorization decisions, or payload-control messages.

## Security and reliability contract

- HMAC-SHA256 authenticates and protects the integrity of both alert and acknowledgement packets.
- Sender ID, receiver ID, protocol version, message type and wall-clock timestamp are signed.
- Each acknowledgement must match both the alert ID and a fresh 128-bit request nonce.
- Packets outside the configured clock-skew window are rejected.
- The receiver remembers delivered alert IDs. An exact retransmission is acknowledged as a
  duplicate; reuse of an alert ID with different content is rejected. The optional SQLite store
  preserves this decision across receiver restarts.
- The aircraft SQLite outbox records an alert before transmission and marks it delivered only after
  a valid correlated acknowledgement.
- Retries and acknowledgement waits are bounded. Delivery failure does not affect Pixhawk or call
  any payload interface.

HMAC provides authentication and integrity, not confidentiality. Use a private/VPN data network or
an encrypted radio link if aircraft position or imagery-derived metadata is sensitive. The two
computers need a trustworthy time source. Production ground operation should enable the SQLite
deduplication database and the downstream ground application should still treat `alert_id` as its
idempotency key.

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

For a real IP radio, replace only the receiver address and validate packet loss, latency, MTU,
reconnect behavior, time synchronization and key rotation on the selected hardware. Serial-only
radios still require a separately tested framing adapter; do not tunnel this protocol blindly over
an unbounded serial stream.

The local Windows HIL has also been exercised as two independent processes using the synthetic
ONNX camera path. The aircraft processed 105 frames, generated one multi-frame-confirmed alert,
received one authenticated ACK and marked its SQLite outbox row `delivered`; the ground receiver
stored the same alert ID and payload hash with no rejection or duplicate. This proves the software
boundary, not GR01 radio performance.

The automated integration suite also covers a ground outage: the first live run exhausts its
bounded attempt and leaves the original alert pending; after the receiver starts, the next live run
retransmits that same alert before reading new frames and marks it delivered only after the signed
ACK. No new alert ID is substituted during recovery.
