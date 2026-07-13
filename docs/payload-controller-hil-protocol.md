# 独立载荷控制器惰性 HIL 协议

## 当前边界

该协议只用于拆桨、约束台架和惰性载荷的软件/HIL 验证。代码实现消息对象、规范化
JSON、HMAC-SHA256、关联校验、状态防重放，以及显式的任务状态机 HIL 适配器；没有串口、
CAN、GPIO、PWM、MAVLink 执行器或实体释放端口。任务状态机内部仍只连接
`FakePayloadPort`，HIL 适配器不能替换或绕过它。

每条消息都必须包含并严格满足：

```json
{
  "simulation_only": true,
  "inert_load_required": true,
  "physical_release_enabled": false
}
```

任何一个值不同都会在解码或对象构造阶段被拒绝。

## 请求绑定

`release_request` 同时绑定：

- 协议版本、消息类型、HMAC key ID 和单调递增序号；
- mission ID、独立模块 ID、唯一 release ID；
- 舱位 ID 和配置允许的载荷类型；
- 已消费的 authorization challenge ID 和 operator ID；
- target ID、target revision、scene digest 和 ruleset version；
- 请求时间和短时失效时间。

控制器侧 `PayloadHilRequestGuard` 只在任务、模块、舱位/类型、时间和序号全部一致时接受
新请求。完全相同的 release ID 和内容属于幂等重传，只能返回缓存结果，不能再次执行。
相同 release ID 的不同内容、序号回退、过期请求或另一舱位仍处于活动状态时均拒绝。

## 控制器反馈

`release_result` 状态为：

```text
ACCEPTED → EXECUTED
         → FAILED

REJECTED、EXECUTED、FAILED 为终态，只允许重复自身状态。
```

反馈绑定同一个 mission、module、release 和 slot，并具有独立的时间戳、序号和 HMAC。
`ACCEPTED` 或 `EXECUTED` 若同时声明控制器或总互锁不健康，会被拒绝。终态回退到
`ACCEPTED`、同序号改变内容、反馈早于请求或反馈过期也会被拒绝。

`EXECUTED` 仅代表控制器报告已执行，不能形成任务成功。现有载荷状态机仍要求第二个、
独立来源的舱位/脱离传感器确认，才能进入 `RELEASE_CONFIRMED`。

## HMAC 规则

- 密钥至少 32 字节，真实密钥不得出现在命令行、报告、Git、截图或审计日志中。
- 请求方向和反馈方向应使用不同 key ID 和不同密钥，并建立轮换和吊销流程。
- JSON 使用 UTF-8、键名排序、无空格分隔并排除 `signature_hmac_sha256` 字段后计算
  HMAC-SHA256；最大消息长度 4096 字节。
- HMAC 只提供消息认证，不能替代硬件总互锁、单舱机械互锁、供电隔离和独立反馈。

## 本机协议演示

```powershell
.\.venv\Scripts\python.exe scripts\payload_hil_protocol_demo.py
```

预期输出必须同时包含：

```text
request_valid=true
accepted_result_valid=true
executed_result_valid=true
idempotency_verified=true
independent_confirmation_still_required=true
port_connected_to_mission=false
physical_release_enabled=false
```

## 真实 UDP 回环 HIL

协议还提供一个默认只绑定 `127.0.0.1` 的 UDP 客户端和惰性控制器模拟器。客户端对同一
签名字节进行有限重试，控制器对幂等 release ID 返回原始缓存结果，不能把重试当成第二次
执行。运行：

```powershell
.\.venv\Scripts\python.exe scripts\payload_hil_udp_loopback_demo.py
```

该演示主动丢弃第一次反馈，预期第二次请求获得缓存的 `ACCEPTED → EXECUTED`：

```text
attempts=2
statuses=["accepted", "executed"]
idempotent_retry_verified=true
command_messages_sent=0
mission_port_connected=false
physical_release_enabled=false
```

控制器模拟器只有显式设置 `simulate_inert_execution=true` 才生成 `EXECUTED`。默认只返回
`ACCEPTED`，客户端最终超时且不会把受理状态当成执行成功。UDP 回环只验证真实 socket、
丢包重试和缓存语义，不代表已经选择真实控制器链路，也不能直接改成面向实体执行器。

## 任务状态机端到端回环

以下命令运行固定翼回放、安全规则、人工授权、`FakePayloadPort` 单舱互锁、认证 UDP HIL
反馈和独立确认的完整软件链路：

```powershell
.\.venv\Scripts\python.exe scripts\payload_mission_hil_loopback_demo.py
```

适配器只能在任务已进入 `DEPLOYMENT_READY` 后调用。任务控制器会先重新计算安全规则、
消费一次性授权并创建唯一 `release_id`，适配器随后把任务、控制器模块、舱位/类型、授权、
目标修订、场景摘要和规则集绑定进请求。`REJECTED` 立即进入确定故障；通信超时或
`FAILED` 进入不确定故障，均禁止自动重试。`EXECUTED` 只推进至 `VERIFYING_RELEASE`，必须
再收到独立舱位传感器确认才能完成。

独立确认使用 `payload_confirmation_hil.py` 中的另一种消息和另一把 HMAC 密钥。消息绑定
mission、release、slot、sensor ID、观测时间和序号，并明确携带 `payload_absent` 与
`sensor_healthy`。允许的 sensor ID 不能与载荷控制器 module ID 相同。篡改、错任务、错
release、错舱位、同源身份、传感器不健康、未离舱、过期、序号回退或同序号改变内容均
不能推进任务；无有效确认时继续等待，最终由释放确认超时进入不确定故障。
`payload_confirmation_udp.py` 提供另一个只接收证据的 UDP 通道；接收器发送的执行命令数
固定为零。它会丢弃无效数据包并继续等到有效确认或有界超时，不能把丢包、篡改包或超时
解释为释放成功。

验收输出应包含：

```text
authorization_bound=true
target_bound=true
statuses=["accepted", "executed"]
independent_confirmation_required=true
independent_confirmation_authenticated=true
controller_and_sensor_id_separated=true
independent_confirmation_udp_datagrams=1
final_payload_state="release_confirmed"
final_mission_phase="return_requested"
command_messages_sent=0
physical_release_enabled=false
```

## 接真实控制器前

1. 固定控制器 MCU、固件版本、模块 ID、舱位和传感器清单。
2. 选择具有完整帧边界、CRC、认证和双向 ACK 的传输；先只接状态和惰性 HIL。
3. 明确 Jetson 与控制器时钟映射，不能直接比较两个设备各自的开机计时。
4. 在控制器断电、重启、消息重复、乱序、篡改、断线、互锁打开和反馈冲突时注入故障。
5. 幂等重传只能返回缓存结果，任何不确定执行都进入锁定故障且禁止自动重试。
6. 完成独立安全评审后，才允许另行设计真实非危险灭火载荷的实体端口。

## 惰性硬件台架只读验收

真实控制器和独立舱位传感器的台架固件应分别输出 JSONL 日志，并使用不同的 source ID、
key ID 和 HMAC 密钥。记录格式见
[`inert-payload-bench-record.schema.json`](../configs/schemas/inert-payload-bench-record.schema.json)。
控制器每个周期记录 `executed` 或 `uncertain`、互锁/控制器健康、固件版本和自动重试次数；
独立传感器使用相同 `cycle_id` 记录 `payload_absent` 和 `sensor_healthy`。两份日志必须分别
签名，不能由 Jetson 合成同一个来源。

验收至少需要：

- 20个 `executed` 周期，每个周期都有独立传感器确认；
- 全部使用惰性载荷，拆桨并清空测试区人员；
- 额外注入至少1个 `uncertain` 周期，且 `automatic_retry_count=0`；
- 控制器固件版本全程一致；
- 日志序号严格递增、cycle ID 唯一、时间新鲜、HMAC有效；
- 不确定周期不能出现传感器成功确认，也不能被计入20次成功周期。

只读验收命令：

```bash
python3 -m multidetect inert-payload-bench-check \
  --controller-log artifacts/payload-controller-bench.jsonl \
  --sensor-log artifacts/payload-sensor-bench.jsonl \
  --controller-hmac-key-env PAYLOAD_BENCH_CONTROLLER_KEY \
  --sensor-hmac-key-env PAYLOAD_BENCH_SENSOR_KEY \
  --bench-id inert-bench-001 \
  --controller-id controller-1 \
  --sensor-id bay-sensor-1 \
  --controller-key-id controller-bench-key-v1 \
  --sensor-key-id sensor-bench-key-v1 \
  --minimum-confirmed-cycles 20 \
  --inert-load-only \
  --people-excluded-from-test-area \
  --out artifacts/evaluation/inert-payload-hardware-bench.json
```

该命令只读文件并验证签名，不创建 socket、串口、CAN、GPIO、PWM 或 MAVLink 控制通道，
输出始终保持 `physical_release_approved=false` 和 `production_approved=false`。
