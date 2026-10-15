# 任务包与地面命令安全

## 威胁清单

| 入口 | 威胁 | 影响 | 现有防护 |
|---|---|---|---|
| `MissionSupervisor.ground_event` | 伪造 abort/pause/resume 或策略更新 | 飞行状态被远程劫持 | 任务包配置的 Ed25519 签名（无 `cryptography` 时为 HMAC-SHA256）、命令签名、任务 ID 绑定 |
| MAVLink `TUNNEL` 操作协议 | 旧命令重放 | 重复执行 abort 或恢复旧策略 | 每个命令带任务 ID、严格递增 sequence 和带容差的本地时间戳 |
| 任务包文件 | JSON/引用/策略被篡改 | 飞行到错误区域或降低失联策略 | 在解析、编译和接受前验证签名；签名覆盖整个规范化 JSON |
| 策略更新 | 降级 link-loss policy | 失联后继续飞行 | Supervisor 只允许收紧策略，且不能延长既有截止时间 |
| 地面链路 | command flood | CPU、日志和状态机耗尽 | 每个任务的滑动窗口限流（默认 20 条/10 秒） |

任务 ID 只是路由字段，不是认证凭据。签名字段和密钥永远不会写入异常消息或飞行日志；日志只记录拒绝原因。

## Python TUNNEL 路径

Python 侧的 MAVLink 入口在 `src/multidetect/operator_mavlink.py`，经
`operator_transport.py` 交给 `operator_protocol.py` 的 `OperatorTunnelCodec`。
Codec 对 TUNNEL 负载做长度检查、sequence/timestamp 编码和截断 HMAC；MAVLink
入口还要求 MAVLink2 signed frame，并校验 source system/component。这个链路层
认证和本页的任务层签名是两层不同的密钥与边界：TUNNEL 的 HMAC/MAVLink 签名
证明帧来自已配置的链路端点，`MissionSupervisor.ground_event` 的签名则证明
具体任务、动作和策略字段没有被改动。收到 TUNNEL 命令后，适配器必须把任务层
字段（`mission_id`、`sequence`、`timestamp`、`signature`）原样传给
`ground_event`；不能只依赖 TUNNEL 的 sequence 或 mission id。

## 密钥和配置

`cryptography` 已安装时使用 Ed25519。操作员用 32 字节 private seed 签署任务包和命令，机载配置文件只放对应的 32 字节 public key（hex、base64 或原始 bytes 均可）。若飞行镜像没有 `cryptography`，同一配置自动使用 HMAC-SHA256；此时文件必须是高熵的预共享密钥，操作员和机载端各保存一份。

在任务配置中设置 `operator_key_path`（也接受 `mission_key_path`、`mission_signing_key_path` 或 `security.key_path`）。路径可为绝对路径或相对仓库根目录。设置该路径后，未签名或验签失败的任务包会在机载接受前拒绝。命令安全参数可放在 `security` 下：`timestamp_window_s` 默认 30 秒、`clock_skew_s` 默认 5 秒、`rate_limit` 默认 20、`rate_window_s` 默认 10 秒。

密钥 provisioning 和 rotation 流程：在离线操作员主机生成新密钥；通过受控介质把 public key（或 HMAC 预共享密钥）写入机载密钥文件并限制权限；先发布使用新 key 的任务包；确认机载日志中的验签成功后再撤销旧 key。轮换期间只能短暂地在配置层允许两个 key，并以任务版本/有效期区分，禁止把 private key、预共享密钥或命令签名放进日志、JSON 示例、命令行参数和仓库。

时间戳使用机载本地时钟并允许 clock skew；没有 GPS 时间时不会把 GPS 当作认证时钟。sequence 在当前 Supervisor 会话内严格递增；重启或换包时应重新签发任务会话，不能复用旧会话命令。

## MAVLink2 链路层签名（互补措施）

应用层签名保护任务语义，MAVLink2 message signing 保护链路上的 MAVLink 帧，两者应同时启用。PX4 中在参数/启动配置启用 MAVLink signing，并把同一个 32 字节链路密钥安全写入对应 MAVLink 实例；QGroundControl 在 Vehicle Setup → Parameters 的 MAVLink signing 页面导入相同密钥、启用 signing，并确认连接状态显示 signed。按 PX4 与 QGC 当前版本的参数名称和 UI 校验后再部署；不要把该链路密钥当作任务包的 operator key，也不要提交到仓库。

## 统一签名合约（Mission Security v2）

任务包与地面命令使用同一份 canonical JSON 合约。签名者先复制完整 JSON 对象，删除 `signature` 与 `signature_alg` 两个字段，保留 `key_id`，然后按以下规则编码：对象键按 Unicode 码点升序排列；数组顺序保持不变；字符串使用 UTF-8 且不转义非 ASCII；数字必须是有限值；不输出空白，分隔符固定为 `,` 和 `:`；最终字节是 UTF-8 编码的 JSON 文本。签名算法和 `key_id` 由本地密钥配置决定，文档中的同名字段只能用于匹配，不能选择验证算法。

密钥文件必须使用 `ed25519-public:`, `ed25519-private:` 或 `hmac:` 前缀，并可用 JSON 对象写成 `{ "algorithm": "...", "key_id": "...", "key": "<prefixed-value>" }`。Ed25519 密钥材料严格为 32 字节；HMAC-SHA256 密钥为 16 至 4096 字节。飞行模式（`mode` 为 `flight`、`real`、`hil` 或 `live`，或显式 `flight_mode=true`）以及任何已配置密钥的任务包都必须带签名；密钥缺失、不可读、权限过宽或签名不匹配时拒绝载入。SIM 模式未配置密钥时仍可载入未签名任务包。

签名字段示例：

```json
{
  "key_id": "operator-2026-a",
  "signature_alg": "ed25519",
  "signature": "<base64>"
}
```

GCS 必须对同一份原始对象执行上述 canonicalization 后再签名，不能先删掉 `key_id`，也不能依赖 Qt 默认 JSON 排序或格式化结果。

地面命令还必须携带版本化时间域：`timestamp_version` 当前固定为 `1`，`timestamp_domain` 只能是 `monotonic` 或 `unix`。`monotonic` 表示发送端的单调时钟值，`unix` 表示 Unix 秒；接收端按照声明的域做 freshness 校验，但链路 liveness 永远使用本地收到命令的 monotonic 时间。远端时间只用于 freshness、远端 heartbeat 顺序和审计。缺少版本或时间域的已签名命令直接拒绝，不能根据数值大小猜测时间域。
