# 任务安全与通信安全审查（2026-10-15）

独立只读审查，经主线复核。处理状态：

- #1、#2（机上侧）、#3、#4、#8、#12、#13：在 `wt/secfix` 修复中（今天新写的任务包签名与监督器代码）。
- #7、#14、#15：在地面站分支 `wt/gcs-ui` 上修，该分支尚未合并。
- #5、#6、#9、#10、#11：位于 `operator_udp.py`、`operator_mavlink.py`、`operator_protocol.py`、`operator_link.py`、`operator_transport.py`，这些文件被 0.3.0-pre.8 发布证据绑定，**暂不修改**，需要所有者决定是否为下一版本重建发布证据后再修。

在问题修复前，**真机飞行不得启用无签名的地面控制，且必须配置任务包签名密钥**。

---

没有修改任何文件；这是只读审计。下面按严重性列出可利用问题，位置均指当前工作树或 `wt/gcs-ui` 分支中的对应文件。

## 高危

### 1. 认证算法由消息字段选择，可把 Ed25519 校验降级为 HMAC

**位置**

- `src/multidetect/mission_security.py:68-75`
- `src/multidetect/mission_security.py:89-107`
- `src/multidetect/mission_security.py:127-144`

`verify_document()` 从未认证的文档字段读取 `signature_alg` / `auth_alg`，然后交给 `_algorithm()`。如果飞机配置的是 32 字节 Ed25519 公钥，攻击者可以构造：

```json
{
  "...": "...",
  "signature_alg": "hmac-sha256",
  "signature": "<HMAC computed with the public key>"
}
```

由于当前代码把同一 32 字节公钥当作 HMAC 密钥，攻击者只需要知道公开的 Ed25519 公钥就可以伪造任务包或 `abort`、`resume`、`policy_update` 等签名事件。

**影响**

攻击者可以伪造安全相关命令，直接改变失联策略、暂停/恢复任务或触发返航；也可以伪造带有不同任务参数的签名任务包。

**修复**

- 密钥加载结果必须带有不可变的密钥类型和算法元数据。
- 算法必须由本地配置决定，不能由待验证文档选择。
- Ed25519 公钥只能验证 Ed25519；HMAC 只能使用明确配置为 HMAC 的密钥。
- 拒绝缺少算法类型、算法与密钥类型不匹配、或带有未知算法的密钥文件。
- 对 HMAC 和 Ed25519 使用不同的 key slot / key identifier，且将 key identifier 绑定进签名域。

---

### 2. 任务包签名不是强制的；飞机端上传路径明确以“无签名”方式加载

**位置**

- `src/multidetect/autonomy/mission_package.py:100-125`
- `src/multidetect/autonomy/mission_supervisor.py:50-53`
- `wt/gcs-ui:src/multidetect/autonomy/mission_operator.py:244-290`，特别是 `269`
- `wt/gcs-ui:ground-control/MultiDetectGCS/custom/src/MultiDetectMissionController.cc:82-125`

`load_mission_package()` 只有在调用方传入 `key` 或 `key_path` 时才验证签名，且 `require_signature=False` 是默认值。分支上的飞机上传处理在 `mission_operator.py:269` 调用：

```python
load_mission_package(strict_json_loads(payload, name="mission package"))
```

没有传入任务包签名密钥，也没有设置 `require_signature=True`。

GCS 端只做本地 schema 校验，然后把解析后的 JSON 重新序列化并通过链路 HMAC 发送。攻击者如果能在上传前修改任务文件，可以改变 `mission_id`、作业区域、预算、计划或失联策略；GCS 会把被修改后的内容作为新的、合法的传输消息签名。传输 HMAC 只能证明“当前 GCS 发出了这份内容”，不能证明“这份内容来自受信任的任务包作者”。

**影响**

任务文件被篡改后仍会被飞机接受。攻击者不需要伪造链路 HMAC，只需在上传前修改本地文件。

**修复**

- 对任务包使用独立的作者签名或签名 manifest，并在 GCS 加载和飞机准入两端都验证。
- 飞机端必须在 `_receive_chunk()` 中使用配置的任务包公钥调用 `load_mission_package(..., require_signature=True, key_path=...)`。
- 缺少密钥、密钥文件不可读或签名缺失时，飞机启动/上传准入必须失败关闭。
- 不要把“链路 HMAC 认证”当作“任务内容来源认证”。
- GCS 和 Python 端使用同一份严格的 canonical JSON 规范；当前 Qt JSON 重序列化和 Python canonicalization 没有形成明确的跨语言签名契约。

---

### 3. 重放状态在重启后全部丢失

**位置**

- `src/multidetect/autonomy/mission_supervisor.py:61-63,131-136`
- `wt/gcs-ui:src/multidetect/autonomy/mission_operator.py:93-100,210-214`
- `src/multidetect/operator_link.py:525-527,681-686,1864-1869,2093-2098`
- `src/multidetect/operator_udp.py:1058-1075`
- `src/multidetect/operator_mavlink.py:127-138`

监督器的 `_last_sequence`、任务操作适配器的 `_last_sequence`、各 command guard 的 replay cache、UDP client 的 status sequence 以及 MAVLink parser 的签名时间高水位都只保存在进程内存中。

飞机或 GCS 重启后，之前捕获的合法 MAVLink/TUNNEL 帧可以再次通过认证。只要命令仍满足其业务条件或 challenge TTL，攻击者即可重放旧的 `abort`、`resume`、策略更新、selection 或 telemetry/status 消息。

**影响**

- 旧命令可在重启后再次执行。
- 旧状态可能被重新显示或重新接受。
- MAVLink signing 的时间回放保护也会因为 parser 重建而重新开始。
- 这直接违反“重启后不能接受捕获帧”的要求。

**修复**

- 持久化每个 operator/session/mission 的 sequence high-water，使用原子写入和崩溃安全提交。
- 更好的做法是每次启动生成持久化 boot epoch / session nonce，并要求新的 authenticated challenge；旧 epoch 的消息全部拒绝。
- MAVLink signing timestamp/replay state 也必须持久化，或用新的链路 session/key epoch 重新建立信任。
- 重启时如果无法读取 replay state，应 fail closed，而不是从零开始。

---

### 4. Epoch 时间戳的 heartbeat 被存入 monotonic 时间字段，导致有效心跳仍被判定失联

**位置**

- `src/multidetect/autonomy/mission_supervisor.py:105`
- `src/multidetect/autonomy/mission_supervisor.py:145-146`
- `src/multidetect/autonomy/mission_supervisor.py:192-195`

代码允许两种时间戳：

- 大于 `100_000_000` 的值被视为 Unix epoch；
- 其他值被视为本机 monotonic 时间。

但是 heartbeat 接受后，无论来源是什么，都执行：

```python
self.last_heartbeat = stamp
```

`connected` 随后用：

```python
self._now - self.last_heartbeat
```

其中 `_now` 是 monotonic 时间。如果 heartbeat 使用当前 Unix 时间，两个时间域相差约数十年，`connected` 会立即变成 `False`。后续评估可能触发失联策略、返航或降落，即使刚刚收到过有效的签名 heartbeat。

**影响**

有效链路被误判为失联，飞机可能错误进入 hold/return/land 路径；这同时可能造成任务无法恢复。

**修复**

- 使用本机 receipt monotonic time 判断链路是否连接。
- 远端 timestamp 只用于新鲜度验证和审计记录，不要直接作为 `last_heartbeat` 的 monotonic 值。
- 消除基于 `100_000_000` 的时间域猜测，在线协议中明确声明 timestamp domain/version。

---

### 5. UDP authenticated peer 可以被捕获帧从另一个源端口替换，造成状态链路拒绝服务

**位置**

- `src/multidetect/operator_udp.py:245-274`
- `src/multidetect/operator_udp.py:267-270`
- `src/multidetect/operator_udp.py:780-799`
- `src/multidetect/operator_udp.py:815-833`
- `src/multidetect/operator_udp.py:280-303`

`serve_once()` 从 UDP 源地址取得 `peer`，然后对任何通过 HMAC/MAVLink 验证的帧调用 `_register_metadata_peer(peer, ...)`。`_register_metadata_peer()` 会替换当前 metadata peer 并清除 heartbeat/待处理状态。

攻击者可以把捕获到的合法 TUNNEL 帧从另一个源端口重放。帧本身的 HMAC 仍然有效，但新的 UDP tuple 会被当作新 peer，随后现有 runtime-status acceptance barrier 被失效，待处理确认也会被取消。

此外，selection 分支 `operator_udp.py:280-303` 没有像 authorization/approach/payload 分支那样要求来源 peer 与当前 challenge peer 匹配。

**影响**

- GCS/Jetson 之间的状态确认被阻断。
- 运行时 acceptance barrier 可能永远无法完成。
- 选择命令可以被转发到任意发送有效认证帧的 UDP 源地址。
- 可通过反复更换源端口持续造成通信抖动。

**修复**

- 将 operator 身份绑定到显式配置的 endpoint、peer identity 或 challenge session，而不是裸 UDP tuple。
- peer 替换必须先经过新的、带 nonce 的 authenticated heartbeat/challenge。
- 所有命令类型，包括 selection，都必须绑定当前 authenticated session/peer。
- 不要因为一个旧的、有效 HMAC 帧来自新 UDP 源端口就替换活动 peer。

---

### 6. malformed-frame flood 会无限增长后台错误队列

**位置**

- `src/multidetect/operator_udp.py:224`
- `src/multidetect/operator_udp.py:963-970`

后台线程捕获每个 `RuntimeError`、`ValueError` 或 `TypeError`，并把错误放入无界 `SimpleQueue`。无线攻击者可以发送大量 malformed MAVLink/TUNNEL 帧，使解析失败路径持续向队列追加对象。

**影响**

内存持续增长，最终可能耗尽 Jetson/GCS 进程内存，阻塞正常的控制和遥测处理。

**修复**

- 使用有界错误队列。
- 队列满时只增加原子 drop counter，不保留每个错误对象。
- 对 malformed frames、同源失败和全局失败分别做 token bucket/rate limit。
- 将重复的解析错误聚合成计数和时间窗口，而不是逐包入队。

---

## 中高危

### 7. Telemetry ACK 在 sequence/replay 检查之前改变状态

**位置**

- `wt/gcs-ui:src/multidetect/autonomy/mission_operator.py:135-158`
- 特别是 `147-158` 与之后 `210-214` 的 sequence 检查

`kind == 36` 的 telemetry ACK 会先执行：

```python
pending.index += 1
pending.attempts = 0
...
```

然后直接返回。通用的 `_last_sequence` 检查位于后面，因此 type 36 完全绕过了它。

攻击者只需重放一个先前捕获的、对应当前 transfer token/index 的合法 ACK，就可以让飞机端跳过 telemetry chunk、清除 pending transfer 或提前完成日志发送。

**影响**

GCS 可能收到不完整的 telemetry/log，或者飞机停止重试，造成操作员失明。该问题也会与重启后的 replay reset 叠加。

**修复**

- 所有消息类型，包括 telemetry ACK，都必须先通过 sequence/window/replay 检查。
- ACK 必须绑定当前 transfer 的 token、index、producer/session epoch 和期望的前一状态。
- 同一 ACK 只能使状态机前进一次。
- replay state 必须持久化。

---

### 8. 有效 sequence 可以任意跳跃；uint32 wrap 没有处理

**位置**

- `src/multidetect/autonomy/mission_supervisor.py:131-136`
- `wt/gcs-ui:src/multidetect/autonomy/mission_operator.py:210-214`
- `src/multidetect/operator_link.py:552-554`
- `src/multidetect/operator_udp.py:1311-1314` 及其他 `receive_*` 方法

当前逻辑通常是“只要 `sequence > last_sequence` 就接受”。攻击者重放一个捕获的高序号消息，可以把 high-water 推到远处，导致之后所有正常但较低的合法消息被拒绝。序号从 `0xffffffff` 回到 `0` 时，简单的 `<=` 比较又会永久拒绝所有新消息，直到重启。

**影响**

可造成控制消息和状态流的长期拒绝服务；重启虽然暂时恢复，却重新打开 replay-after-reboot 漏洞。

**修复**

- 使用持久化 extended counter 或明确的 authenticated epoch。
- 对 sequence 使用 wrap-aware serial arithmetic。
- 限制可接受的最大 forward jump，异常跳跃触发重新握手，而不是永久推进 high-water。
- 让 sequence 状态按 session/peer/type 隔离，避免单一全局计数器扩大影响面。

---

### 9. 签名状态包没有应用层 timestamp freshness 检查

**位置**

- `src/multidetect/operator_protocol.py:1300-1333`
- `src/multidetect/operator_udp.py:1301-1470`
- `wt/gcs-ui:src/multidetect/autonomy/mission_operator.py:135-146`

outer HMAC 覆盖了 `sent_ms`，但 codec 只是把它转换为 `sent_at_s`，没有检查它是否过旧或来自未来。UDP client 的 `receive_*` 方法只比较每类 status 的 sequence，不比较 `sent_at_s` 和本地接收时间。

因此，旧的、合法捕获状态可以在重启后或长时间延迟后再次被接受和显示。mission operator 的 `handle()` 也没有检查 `DecodedOperatorPacket.sent_at_s`。

**影响**

操作员可能看到已过时的 policy/action/remaining-budget/status；在安全决策依赖这些状态时会造成错误判断，且攻击者可通过重放保持“看起来有状态”的假象。

**修复**

- 使用 authenticated producer boot epoch/session nonce。
- 对 status 设置最大年龄和未来容忍窗口。
- 对无法可靠比较 wall clock 的链路，用 challenge-relative monotonic freshness 或 session sequence，而不是 GPS/Unix 时间。
- 状态过期时必须明确标记为 stale，不得继续作为当前状态使用。

---

### 10. MAVLink signing link ID 未在接收端强制校验

**位置**

- `src/multidetect/operator_mavlink.py:127-138`
- `src/multidetect/operator_mavlink.py:330-366`

发送端配置了 `signing_link_id`，但接收 parser 只设置了 secret key，没有设置/验证期望的 link ID。接收路径检查 signed、源 system/component、TUNNEL target 和 payload type，却没有检查签名所属 link。

如果同一 MAVLink signing key 被多个链路复用，另一个 link 上的合法捕获帧可以被当前 endpoint 接受。

**影响**

跨链路重放和链路混淆；在共享 key 的部署中，来源绑定失效。

**修复**

- 接收 parser 强制要求配置的 signing link ID。
- 将 link ID、channel identity 和 endpoint identity 绑定到 key provisioning。
- 最好每条无线链路使用独立 signing key。

---

### 11. 有效 authenticated command 没有速率限制，重复帧可造成 ACK/CPU 放大

**位置**

- `src/multidetect/operator_udp.py:275-279`
- `src/multidetect/operator_udp.py:280-303`
- `src/multidetect/operator_transport.py:92-140`

每个通过认证的命令都会递增全局 ACK sequence 并发送 ACK。重复 selection 即使命中 cache，也会再次进入处理/回复路径。攻击者可以重放大量捕获帧，消耗解析、cache、ACK 发送和网络带宽。

**影响**

即使 replay guard 最终拒绝命令，仍然可以通过 ACK amplification 和 CPU 消耗拖慢正常控制；与 peer hijack 和无界错误队列结合时影响更大。

**修复**

- 按 peer/session/type 设置 token bucket。
- 对重复 token/command 设置更严格的重放回复节流。
- ACK sequence 不应因为未经 rate gate 的垃圾流无限消耗。
- 为 command processing 设置有界队列和每周期预算。

---

## 中危

### 12. Supervisor 的 rate limit 在状态改变之后才执行，且 unsigned 模式完全没有限制

**位置**

- `src/multidetect/autonomy/mission_supervisor.py:168-214`

`policy_update`、heartbeat、abort、pause、resume 会先改变内部状态，之后才检查 `_event_times`。例如：

- 超限的 abort 仍可能先调用 `_escalate("return_now", ...)`；
- 超限的 heartbeat 仍会更新 `last_heartbeat`；
- 超限的 pause/resume 仍会改变暂停状态。

同时，整个 rate-limit block 位于 `if secured:` 内；`security_key=None` 时没有任何事件限速。

**影响**

有效但过量的消息可以改变安全状态，即使调用者最终得到 `False`。在 unsigned 配置下，radio flood 可以持续刷新 heartbeat 或反复操作控制状态。

**修复**

处理顺序应为：

1. 解析和基础长度检查；
2. 身份/签名验证；
3. timestamp 和 sequence/window 验证；
4. rate-limit gate；
5. 只有通过前四步后才改变状态；
6. 最后原子提交 high-water 和事件记录。

即使是显式 insecure/test 模式，也应保留资源保护限速；飞行配置则应直接禁止 unsigned 控制。

---

### 13. 密钥格式解析会丢失算法类型，并把错误输入静默降级为 HMAC

**位置**

- `src/multidetect/mission_security.py:34-65`
- `src/multidetect/mission_security.py:68-75`

`_decode_key()` 会删除 `ed25519-public:`、`ed25519-private:`、`hmac:` 前缀，只返回字节。随后 `_algorithm()` 仅根据长度推断算法：32 字节就是 Ed25519，否则就是 HMAC。

因此：

- `hmac:<32 bytes>` 会被当成 Ed25519；
- 本来应该是 Ed25519 的格式错误或长度错误可能被当作 HMAC；
- 任意 malformed key material 会通过 fallback 继续运行；
- 没有检查文件权限、owner 或私钥/对称密钥的保护级别。

**影响**

密钥配置错误可能改变认证算法或导致认证失效；更危险的是，运维人员以为使用了 Ed25519，实际部署可能落入 HMAC 路径。密钥文件泄露风险也没有被检测。

**修复**

- 返回 `(key_type, key_bytes, key_id)`，不要丢弃前缀语义。
- 对每种算法严格检查长度和格式。
- 错误 key 必须拒绝，禁止静默 fallback。
- 在飞机上检查私钥/HMAC 文件的权限和 owner；权限不符合要求时 fail closed。
- 启动日志只能记录 key id/type，不得记录原始 key 或异常中的 key 内容。

---

### 14. C++ telemetry reassembly 可被四个长期未完成的 transfer 占满

**位置**

- `wt/gcs-ui:ground-control/MultiDetectGCS/custom/src/MultiDetectMissionController.cc:291-337`
- `wt/gcs-ui:ground-control/MultiDetectGCS/custom/src/MultiDetectMissionController.cc:235-240`

GCS 只允许 `_assemblies` 中同时存在四个 transfer，但每收到一个合法 chunk 就刷新 `started`。因此，攻击者只要重放四个不同 token 的已捕获 telemetry chunk，并在 30 秒内持续发送，就可以让四个 assembly 永不超时。第五个 transfer 会在：

```cpp
if (_assemblies.size() >= 4)
    return;
```

处被静默丢弃。

同时，GCS 对每个 chunk 的 ACK 在最终 hash/schema 验证之前就发送；完整 transfer 只有最后才进行 SHA-256 和 JSON 校验。

**影响**

合法遥测和日志无法完成重组，操作员长期失去状态可见性。攻击者不需要伪造 HMAC，只需重放此前捕获的合法分片。

**修复**

- 为每个 transfer 设置固定的总时限，而不是只在收到 chunk 时刷新 idle timer。
- 限制每个 peer、mission 和时间窗口的 transfer 数量及总字节数。
- 超过上限时驱逐最旧的未完成 assembly。
- 将 transfer token、producer epoch 和 sequence 绑定起来，拒绝旧 transfer 重放。
- 只有在完整 payload 通过 digest 和 schema 验证后，才发送最终完成确认；中间 ACK 必须不能让发送端误以为整个 transfer 已被接受。

---

### 15. C++ telemetry JSON 只做 QJsonDocument 解析，没有复用严格的重复键/规范校验

**位置**

- `wt/gcs-ui:ground-control/MultiDetectGCS/custom/src/MultiDetectMissionController.cc:337-350`
- 对比 `MultiDetectMissionPackage.cc:19-123,189-205`

`MultiDetectMissionPackage::parse()` 有 `uniqueKeys()` 检查，但 `_adopt()` 对 telemetry 直接调用 `QJsonDocument::fromJson()`。Qt 对重复 JSON object key 的语义可能保留最后一个值。虽然 payload hash 会检查字节完整性，但不会消除重复键带来的语义歧义。

**影响**

如果飞机端或中间组件产生重复键，GCS 展示的字段可能与其他实现解析出的字段不同。对安全状态、policy/action 或预算字段来说，这会造成跨实现状态解释不一致。

**修复**

- telemetry 也使用严格 JSON parser，拒绝重复 key。
- 对 status/log 使用固定 schema、固定类型和固定字段白名单。
- 如果跨语言签名/摘要依赖 JSON，统一 canonicalization 和 duplicate-key 规则。

---

## 已确认的正向控制

以下路径没有发现对应的 v1 downgrade 或明显的长度解析绕过：

- `src/multidetect/operator_protocol.py:1300-1323` 严格检查 magic、version、flags、reserved、body length，并对完整 header/body 做 HMAC。
- `src/multidetect/operator_protocol.py:1313` 会拒绝非当前协议版本，当前路径没有发现 v1 绕过 v2 检查的 fallback。
- `src/multidetect/operator_protocol.py:1463-1479` 对发送帧也执行固定长度和 TUNNEL 容量限制。
- `src/multidetect/operator_mavlink.py:330-376` 拒绝 unsigned MAVLink，并校验 source system/component、target system/component、payload type 和 payload length。
- Python v2 mission wire 的 `struct.unpack`、`count/total/index` 和 chunk data length 检查总体完整，`mission_operator.py:266-269` 会在 JSON parse 前先验证完整 payload 长度和 SHA-256。
- C++ `MultiDetectMissionWire::decode()` 对 chunk body、ACK、control body 和 telemetry ACK 做了上限及精确长度检查。
- HMAC 比较使用了 `hmac.compare_digest()`。
- 当前审计的 operator protocol 文件中没有额外的 fragment overlap/reassembly 路径；分片风险主要集中在 gcs-ui 的 mission telemetry/package adapter。

没有运行会修改文件的命令，也没有改动工作树。