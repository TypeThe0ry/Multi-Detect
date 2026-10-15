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
