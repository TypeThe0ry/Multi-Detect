# G20 + GR01 + Jetson + Pixhawk V6X 集成方案

## 已从说明书确认的边界

本方案按用户更正统一使用 **GR01 接收机**。

- 网口三体摄像头输出固定为 RTSP/H.265、720P@30FPS，默认地址为
  `rtsp://192.168.144.108:554/stream=0`；相机 IP 与网关可修改。来源：
  《三体摄像头网口版》手册第 7、11 页。
- GR01 只有 1 个网口、1 个数传口、1 路 SBUS 和 3 路 PWM；数传串口支持
  57600/115200/921600。无线链路标称上行 200Kbps~1.2Mbps、下行
  10Mbps~20Mbps。来源：《G20 产品说明书 V1.0》第 5、20 页。
- G20 是 Android 13 设备，厂商声明提供 SDK，允许安装第三方 App；标准接法是
  GR01 网口连相机、数传口连飞控。G20 可用自定义 RTSP 地址显示第三方相机视频。
  来源：《G20 产品说明书 V1.0》第 2、14、15 页。

手册没有声明相机允许几个并发 RTSP 客户端，也没有保证 GR01 空中端网口可承载
Jetson 发往 G20 的任意双向 IP 数据。这两项必须台架验证，不能当成已确认能力。

## 推荐拓扑

GR01 只有一个网口，因此相机、Jetson 和 GR01 之间使用现有机载交换机：

```text
                         aircraft Ethernet 192.168.144.0/24
RTSP camera ─────────────┐
                         ├── unmanaged switch ─── GR01 Ethernet ⇄ radio ⇄ G20
Jetson Orin Nano ────────┘
       │
       └── UART, read-only first ─── Pixhawk V6X TELEM2

GR01 data+SBUS ───────────────────── Pixhawk V6X TELEM1 + RC input
```

建议先保留三个相互独立的平面：

1. **视频平面**：摄像头 RTSP 同时由 Jetson 和 G20 拉取。Jetson 不重编码，G20
   本地显示原始 H.265 视频。
2. **视觉元数据平面**：G20 定制 QGC 与 Jetson 直接通过 GR01 以太网链路交换
   MAVLink2 `TUNNEL` 消息。目标框、状态和健康信息不经过 Pixhawk。
3. **飞控平面**：G20/GR01 与 Pixhawk 传输标准飞控 MAVLink；Jetson 初期只读
   TELEM2。任何飞控写入能力都单独评审和测试。

优先使用直连视觉元数据平面的原因：ArduPilot 可以在遥测端口间路由它理解的
MAVLink 消息，但不能把“任意未知自定义消息必然转发”作为设计前提。标准
`TUNNEL` 消息是 MAVLink 为组件间传输扩展数据提供的容器。开发阶段使用本项目
私有的 `payload_type > 32767`，产品化前再申请正式类型。参考：

- <https://ardupilot.org/dev/docs/mavlink-routing-in-ardupilot.html>
- <https://mavlink.io/en/services/tunnel.html>

如果台架证明 GR01 的空中端网口不能发送上行 IP 数据，备选方案才是：

```text
G20 custom QGC → MAVLink2 TUNNEL → GR01 UART → V6X TELEM1
→ ArduPilot routing → V6X TELEM2 → Jetson
```

该备选方案需要先在 ArduPlane SITL 验证，再在拆桨、无载荷的 V6X 台架上抓包验证
定向 `TUNNEL` 是否双向路由、丢包率和延迟。不要直接使用飞控不认识的自定义消息 ID。

## 框选与跟踪协议

G20 触摸框不能只有四个浮点数。每条命令至少绑定：

- 协议版本、会话 ID、单调递增序号和唯一命令 ID；
- `stream_id`、源视频宽高、旋转方向；
- G20 正在显示的帧 ID（若视频栈可以取得）和短时有效期；
- 归一化 `x1, y1, x2, y2`；
- `SELECT`、`SWITCH` 或 `CANCEL` 动作。

Jetson 必须拒绝错误视频流、错误分辨率/旋转、过期、未来时间、重复 ID 和序号回退
的命令。对应的纯软件模型已加入
[`operator_link.py`](../src/multidetect/operator_link.py)，其中框选命令最长只允许 5 秒，
并且它明确**不等同于载荷部署授权**。

紧凑载荷的HMAC、定向地址、有限重试与幂等ACK分别由
[`operator_protocol.py`](../src/multidetect/operator_protocol.py)、
[`operator_transport.py`](../src/multidetect/operator_transport.py) 和
[`operator_mavlink.py`](../src/multidetect/operator_mavlink.py) 实现。MAVLink适配层只生成
或接收指定源/目标组件的 `TUNNEL` 消息，拒绝 HEARTBEAT、COMMAND、MISSION 等所有
其他消息；当前仍只是字节帧编解码，不会打开UDP或串口，也不会写入Pixhawk。

跟踪返回数据包括：

- 原选择命令 ID、状态序号、Track ID；
- `INITIALIZING / TRACKING / LOST / CANCELLED / REJECTED`；
- 归一化目标框、类别、置信度、跟踪质量；
- Jetson 源帧 ID、采集时间、结果时间；
- 可选相对方位与估算距离，并在不可用时发送空值而不是伪造数值。

MAVLink 不保证消息必达，因此选择命令需要 ACK、有限重试和幂等处理。跟踪状态是
最新值流，旧序号直接丢弃；无需可靠重传每一个旧框。目标元数据建议 5~10Hz，
健康状态 1Hz，避免占满 GR01 标称较低的上行带宽。

当前软件已经实现两层认证和失败闭锁：

- 内层应用帧最大 128 字节，使用 HMAC-SHA256 截断 128 位标签；选择帧 98 字节，
  跟踪状态帧最大 121 字节，并支持最长 16 个 UTF-8 字节的类别名；
- 外层使用签名 MAVLink2 `TUNNEL`，实验 `payload_type=42000`，并校验源/目标
  system ID、component ID、签名时间戳和重放；
- G20 选择命令的重传字节保持一致，但每次由 MAVLink2 生成新的签名时间戳；Jetson
  对完全相同的命令返回相同语义 ACK，对相同命令 ID 的不同内容拒绝；
- 默认组件规划为 G20 `255/190`、Jetson `1/191`，上真机前需与现有 QGC、飞控和
  其他伴随组件的 ID 清单核对，避免冲突。

无需硬件即可运行回环：

```powershell
multi-detect operator-link-demo
```

该命令模拟一个选择包和一个 ACK 丢失，验证第三次尝试完成幂等确认，然后返回跟踪框。
它不会打开串口、UDP、飞控或任何载荷接口。真实产品的应用 HMAC 密钥和 MAVLink2
签名密钥必须分别配置、轮换并保存在设备安全存储中，不能使用演示密钥。

### GR01 双向 IP 台架命令

先在 Jetson 与测试用 G20/电脑上安全配置相同的两个密钥：应用 HMAC 密钥至少 32
字节，MAVLink2 密钥为恰好 32 字节对应的 64 位十六进制字符串。不要把密钥写进命令
参数、仓库、截图或日志。

Jetson 端：

```bash
export MULTIDETECT_OPERATOR_KEY='<secure application key>'
export MULTIDETECT_MAVLINK_KEY_HEX='<64 hex characters>'
multi-detect operator-udp-server \
  --bind-host 0.0.0.0 --port 14580 \
  --operator-hmac-key-env MULTIDETECT_OPERATOR_KEY \
  --mavlink-signing-key-hex-env MULTIDETECT_MAVLINK_KEY_HEX \
  --max-datagrams 1
```

G20 定制 App 完成前，可先在同网段 Windows 电脑代替 G20：

```powershell
multi-detect operator-udp-select --host <JETSON_IP> --port 14580 `
  --operator-hmac-key-env MULTIDETECT_OPERATOR_KEY `
  --mavlink-signing-key-hex-env MULTIDETECT_MAVLINK_KEY_HEX `
  --x1 0.32 --y1 0.21 --x2 0.61 --y2 0.72
```

验收输出必须同时满足：客户端 `accepted=true`，服务端 `accepted=true`，命令 ID
一致，正常链路 `attempts=1`。随后至少循环 100 次，统计 ACK 延迟、重试次数、超时、
错误签名和序号拒绝；同时用抓包确认只有 UDP/14580 元数据，没有任何飞控命令。
防火墙只允许指定 G20/Jetson 地址访问该端口，测试结束后关闭诊断服务。

## G20 定制 QGC 界面

QGC 官方自定义构建支持 Android、自定义 Fly View overlay、QtQuick 模块和相机管理器，
适合在 G20 上实现本地叠加。参考：

- <https://docs.qgroundcontrol.com/master/en/qgc-dev-guide/custom_build/custom_build.html>
- <https://docs.qgroundcontrol.com/master/en/qgc-user-guide/fly_view/video.html>

建议的飞行主界面：

```text
┌──────────────────────────────────────────────────────────────────────┐
│ AUTO  GPS 17  Link 82%  H 120m  AS 24m/s │ Jetson 28FPS 54°C │ SAFE │
├───────────────────────────────────────────────┬──────────────────────┤
│                                               │ 目标 #42  flame      │
│       原始 RTSP 视频 + G20 本地绘制目标框     │ 置信度 91%           │
│                                               │ 跟踪质量 87%         │
│       拖动框选；点击候选框切换目标            │ 状态 TRACKING        │
│                                               │ 安全规则 6/8         │
│                                               │ 人员净空：未知       │
├───────────────────────────────────────────────┴──────────────────────┤
│ [上一目标] [取消锁定] [下一目标] │ [巡检告警] [查看部署建议]         │
└──────────────────────────────────────────────────────────────────────┘
```

颜色只表达状态，不单独承担安全含义：候选目标黄色、稳定跟踪青色、目标丢失灰色、
安全拒绝红色、已满足规则但未授权为蓝色。载荷授权按钮不放在普通跟踪工具栏中，必须
进入独立的“部署确认”抽屉，避免误触。

## 非危险灭火载荷部署闭环

这里使用“火区跟踪、部署区域计算、载荷部署控制”，不把 YOLO 输出直接接到释放机构。

```text
检测火/烟 → 多帧确认 → 火区跟踪 → 计算候选部署区域
→ 人员/车辆排除 + 地理围栏 + 高度/姿态/链路/库存检查
→ G20 显示证据与拒绝原因 → 操作者短时授权
→ 授权窗口内持续复核 → 独立载荷控制器互锁执行
→ 执行反馈 + 独立舱位传感器确认 → 继续巡检或返航
```

“自动”限定为自动发现、跟踪、排序和生成候选部署窗口。物理释放必须满足：

- 授权绑定任务、Track ID、场景摘要、规则版本、载荷舱位和有效期；
- 任一人员净空、地理围栏、飞行包线、链路或载荷清单变为未知/失败，立即撤销授权；
- 操作者授权后只允许在很短的有效窗口内请求一次；禁止连续释放；
- 独立控制器保持物理锁、单舱互锁、执行反馈和第二传感器确认；
- UI 永远提供取消和锁定状态，不允许模型直接发出释放命令。

现有仓库仍只有 `FakePayloadPort`，没有 GPIO、CAN、串口、PWM 或 MAVLink 物理释放
路径。真实控制器只能在惰性载荷 HIL、失效注入和独立安全评审通过后接入。

## 第一次台架验收顺序

1. 拆桨、无载荷，用现有交换机连接相机、Jetson、GR01；确认三者 IP 不冲突、交换机
   供电稳定且不会把 PoE 电压误送给非 PoE 设备。
2. G20 与 Jetson 同时拉取 `stream=0` 30 分钟，记录是否拒绝第二客户端、码率、丢帧、
   端到端延迟和相机温度。
3. 从 Jetson 经 GR01 以太网向 G20 运行
   [`data-link-alerts.md`](data-link-alerts.md) 的认证UDP/ACK回环，再反向测试；确认是否
   真正双向，并记录时延、丢包和MTU。
4. 直连可用时跑 MAVLink2 `TUNNEL` 回环：命令 ACK、序号、过期、断链重连和签名。
5. 仅接 Pixhawk 标准遥测，与 QGC 数值逐字段比对；此阶段不发飞控命令。
6. 用录制视频测试触摸框选、切换、取消、目标丢失和视频比例变化。
7. 最后才使用 `FakePayloadPort` 跑“安全规则 → 授权 → 仿真反馈”闭环。

还需向厂商确认或实测：相机 RTSP 并发数、GR01 以太网是否双向透明、G20 SDK/API、
QGC APK 签名安装方式、H.265 硬解支持、GR01 实际上/下行延迟及丢包。相机手册对
输入电压在参数页和操作页存在 13~80V 与 7.2~80V 的不一致，供电设计前应让厂商
书面确认并使用独立稳压与保护。
