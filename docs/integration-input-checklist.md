# 集成输入清单

按以下顺序准备即可，不需要一次提供全部硬件。密码、HMAC密钥、私钥和完整RTSP凭据不要粘贴到聊天、Git或审计文件中。

## 分阶段证据门禁

每一阶段都把原始验收结果保存为 JSON，再用 SHA-256 绑定到证据包。检查器会读取并重新
验证原始结果中的事件类型、硬件/仿真声明、最低样本量、关键失败闭锁指标和时间新鲜度，
不会只相信证据包里写了“通过”。

当前支持的递进档位：

| 档位 | 必须具备的证据 |
| --- | --- |
| `software_hil` | 本机组合软件 HIL |
| `vision_bench` | 软件 HIL + 真实 RTSP 摄像头 + Jetson Orin Nano |
| `airframe_bench` | 上述全部 + Pixhawk V6X + GR01 |
| `inert_payload_bench` | 上述全部 + 无危险物惰性载荷台架 |

验证现有软件 HIL 示例：

```powershell
.\.venv\Scripts\python.exe -m multidetect integration-evidence-check `
  examples\integration_evidence.software_hil.json `
  --profile software_hil `
  --out artifacts\evaluation\integration-evidence-software-hil.json
```

证据包格式由
[`integration-evidence.schema.json`](../configs/schemas/integration-evidence.schema.json) 定义。
硬件档位默认拒绝超过 168 小时的证据，可用 `--maximum-hardware-age-hours` 收紧。软件 HIL
文件即使复制到硬件记录中，也会因事件类型、`hardware_observed` 和 `simulation_only` 不符
而失败。任何档位通过后，检查结果仍固定为 `production_approved=false` 和
`physical_release_approved=false`；它只是集成台架阶段门禁，不是适航、现场或实体投放批准。

## A. 第一批：恢复真实视觉检测

### A1. 火烟ONNX模型

放置位置：

```text
models/fire-smoke-nms.onnx
models/fire-smoke-nms.manifest.json
```

必须满足：

- ONNX输入为一张RGB图像，推荐 `1 x 3 x 640 x 640` NCHW。
- 模型已经包含解码和NMS。
- 输出形状为 `N x 6` 或 `1 x N x 6`。
- 每行字段严格为 `x1, y1, x2, y2, confidence, class_id`。
- 明确坐标是 `normalized_xyxy` 还是 `letterbox_xyxy_px`。
- 类别顺序明确，例如 `0=fire, 1=smoke`；运行时会把 `fire` 规范化为 `flame`。
- 提供来源、版本、SHA-256、导出环境、类别、输入输出和批准状态清单。

拿到模型后首先运行：

如果ONNX来源明确但还没有清单，先生成一个永远处于 `quarantined`、未批准状态的候选清单：

```powershell
.\.venv\Scripts\python.exe -m multidetect model-manifest-init `
  --onnx-model models\fire-smoke-nms.onnx `
  --out models\fire-smoke-nms.manifest.json `
  --model-id fire-smoke-candidate `
  --model-version candidate-v1 `
  --source-description "填写训练仓库、提交、数据与导出来源" `
  --class-names fire,smoke `
  --output-coordinates normalized_xyxy
```

该命令自动计算SHA-256，但不会证明许可证、数据权利或准确率，也不会把模型标成生产批准。随后执行：

```powershell
.\.venv\Scripts\python.exe -m multidetect model-check `
  --onnx-model models\fire-smoke-nms.onnx `
  --model-manifest models\fire-smoke-nms.manifest.json `
  --class-names fire,smoke `
  --output-coordinates normalized_xyxy `
  --provider CPUExecutionProvider
```

如果手里只有上游 `best.pt`，不要在开发机或Jetson直接运行。先只做字节校验；该命令不会导入PyTorch，也不会反序列化pickle：

```powershell
.\.venv\Scripts\python.exe -m multidetect legacy-checkpoint-verify C:\隔离暂存路径\best.pt
```

默认核对已审计上游文件的14,758,954字节和固定SHA-256。只有完全匹配，才可考虑进入隔离、断网、无凭据的导出流程，详见 [`models/README.md`](../models/README.md)。匹配也不代表文件可以在本机直接运行。

### A2. 验证视频

至少准备以下短视频或抽帧数据，并确认有权用于测试：

- 白天明火。
- 烟雾但无可见明火。
- 无火正常道路、树林和建筑。
- 夕阳、红色灯光、反光等易误报场景。
- 雾、云、扬尘或蒸汽。
- 条件允许时增加夜间、远距离、小目标和相机抖动。

每个样本需要标注真实类别与边界框，才能使用 `evaluate-detections` 计算精确率、召回率、误报和漏检。

### A2.1 安全对象模型

载荷构型还需要独立治理的人员/消防员检测模型，清单必须声明
`model_role=safety_object_evidence`。仅把类别写成 `person` 不会让普通模型通过角色门禁。
该模型只向失败闭锁规则提供证据，不能直接确认人员净空、授权或触发载荷动作。

### A3. RTSP摄像头信息

准备但不要公开密码：

- 厂商和型号。
- 编码：H.264或H.265。
- 分辨率、帧率、码率。
- RTSP路径格式，可用 `USER:PASSWORD@HOST` 占位。
- 摄像头到Jetson是网线、交换机还是板载网络。
- 固定安装方向：前视、前下视或垂直下视。
- 水平/垂直视场角；后续定位火区时还需要内参和安装外参。

先在Jetson本地用环境变量或受限配置注入真实URI，然后执行：

```bash
export MULTIDETECT_RTSP_URI='rtsp://USER:PASSWORD@CAMERA_HOST:554/STREAM'
python3 -m multidetect camera-check \
  --source-env MULTIDETECT_RTSP_URI \
  --rtsp-transport tcp \
  --frames 300
```

单次读帧检查通过后，生成可供 `vision_bench` 门禁读取的持续台架证据：

```bash
python3 -m multidetect camera-bench \
  --source-env MULTIDETECT_RTSP_URI \
  --rtsp-transport tcp \
  --minimum-frames 300 \
  --minimum-duration-seconds 60 \
  --maximum-duration-seconds 120 \
  --out artifacts/evaluation/rtsp-camera-bench.json
```

该命令持续到帧数和时长两个条件都满足，记录分辨率稳定性、平均 FPS、P50/P95 读帧
延迟、重连次数和失败次数。证据文件不包含 URI、主机、用户名或密码；断流、分辨率变化、
超时或未取得任何帧都会失败闭锁。本机 `--source 0` 可以验证采集代码，但产生的是
`local_camera_bench_passed`，不能满足必须为真实 RTSP 的硬件档位。

## B. 第二批：Jetson Orin Nano

在Jetson仓库目录运行：

```bash
bash scripts/collect_jetson_info.sh > artifacts/jetson-info.txt
```

检查输出后再提供。脚本只采集系统、JetPack/CUDA/TensorRT/Python、磁盘、功耗模式和串口设备信息，不读取RTSP密码、SSH密钥或环境变量内容。

还需要说明：

- Jetson Baseboard具体型号和接口图。
- 可用电源电压、持续功率和散热方式。
- 计划使用原生系统还是NVIDIA容器。
- 期望巡航时长和允许的Jetson功耗模式。

完成短 RTSP 检查后，用当前候选模型执行30分钟采集+推理浸泡：

```bash
python3 -m multidetect jetson-vision-bench \
  --source-env MULTIDETECT_RTSP_URI \
  --rtsp-transport tcp \
  --onnx-model artifacts/training/hardneg-snapshots/v5-local-calibrated/best.onnx \
  --model-manifest artifacts/training/hardneg-snapshots/v5-local-calibrated/best.manifest.json \
  --class-names flame,smoke \
  --output-coordinates letterbox_xyxy_px \
  --provider TensorrtExecutionProvider \
  --provider CUDAExecutionProvider \
  --provider CPUExecutionProvider \
  --trt-engine-cache /var/lib/multi-detect/trt-cache \
  --minimum-frames 1000 \
  --minimum-duration-seconds 1800 \
  --maximum-duration-seconds 2100 \
  --maximum-temperature-c 95 \
  --out artifacts/evaluation/jetson-orin-nano-bench.json
```

该命令从 `/proc/device-tree/model` 识别设备，从 Linux thermal zones 采集温度，并记录实际
ONNX Runtime provider。设备不是 Jetson Orin Nano、首选 provider 回退到 CPU、温度不可读或
超过上限、推理异常、断流、分辨率变化以及浸泡未达标均会失败。它不显示或保存图像，不记录
RTSP URI，也不启用飞控或载荷接口。

## C. 第三批：Pixhawk V6X只读接入

准备：

- PX4或ArduPilot，以及准确固件版本。
- Jetson连接到哪个TELEM端口。
- 串口设备名，例如 `/dev/ttyTHS1` 或 `/dev/serial/by-id/...`。
- 波特率，常见起点为57600，但必须以飞控配置为准。
- QGroundControl中对应端口和MAVLink配置。
- 接线电平、TX/RX交叉、公共地和供电方案。

先只运行：

```bash
python3 -m multidetect pixhawk-check \
  --endpoint /dev/ttyTHS1 \
  --baud 57600 \
  --samples 20 \
  --require-fresh-link
```

该阶段不上传任务、不切换模式、不发送心跳或飞控命令。需要把结果与QGroundControl逐字段核对。

短检查通过后，复制
[`qgc_telemetry_snapshot.template.json`](../examples/qgc_telemetry_snapshot.template.json)，在机体静止、
桨叶拆除且不解锁的台架状态下，填写 QGC 同一时段显示的经纬度、相对高度、航向、地速、
横滚、俯仰、电池、卫星数、解锁状态、模式和任务序号。时间戳必须是当前 UTC，格式由
[`qgc-telemetry-snapshot.schema.json`](../configs/schemas/qgc-telemetry-snapshot.schema.json) 定义。
然后执行：

```bash
python3 -m multidetect pixhawk-v6x-bench \
  --endpoint /dev/ttyTHS1 \
  --baud 57600 \
  --qgc-snapshot artifacts/qgc-v6x-current.json \
  --minimum-samples 100 \
  --sample-interval-seconds 0.2 \
  --stale-after-seconds 1.0 \
  --maximum-qgc-age-seconds 120 \
  --out artifacts/evaluation/pixhawk-v6x-bench.json
```

通过条件包括：100份连续新鲜的心跳+位置样本、所有 QGC 字段在固定容差内、提供器保持
只读、`messages_transmitted_by_jetson=0`，以及在不接收新消息的情况下缓存超过 stale
时限后 `link_healthy` 和 `position_healthy` 同时变为 false。该检查不发送心跳、参数请求、
任务、模式、命令或执行器消息；真实断电/拔线和 Pixhawk 自身 failsafe 仍应在后续有人监护的
硬件测试中单独验证。

### C2. 独立区域安全源 HIL

Pixhawk 通用遥测不会自动证明允许区域、围栏健康或投放区净空。先准备一个运行在 Jetson
同一主机上的受信任只读桥接进程，提供：

- 区域源唯一 ID、协议版本和 HMAC key ID。
- 与任务配置完全一致的 mission ID。
- 来自当前 Pixhawk 位置附近的经纬度，默认最大偏差 25 米。
- Jetson 单调时钟时间戳和严格递增序号。
- `in_allowed_zone`、`geofence_healthy`、`release_zone_clear` 三个独立布尔条件。
- 原子文件替换，避免程序读取到半写入 JSON。

实时接入参数和签名 JSON 结构见
[`live-camera-jetson.md`](live-camera-jetson.md#独立区域安全证据仅文件型-hil)。先注入篡改、
过期、位置偏差、错误任务、序号回退和文件消失故障，确认全部回到未知并禁止授权。真实硬件
区域源仍需单独验证时钟映射、密钥保护和传输故障，不能把此文件 HIL 视为现场批准。

## D. 第四批：数传告警

需要决定接收端协议：

- MAVLink伴随消息、UDP应用协议、串口电台协议或其他经过批准的链路。
- 最大消息长度、带宽、单向/双向、丢包特性。
- 是否支持远端ACK和事件ID去重。
- 重试上限、退避时间和断链策略。

软件已经提供 HMAC-SHA256 UDP、双向关联ACK、有限重试、SQLite飞端发件箱和地面端
持久化去重。它们已通过本机真实UDP socket测试，但仍不代表GR01实际链路可用。台架
阶段按 [`data-link-alerts.md`](data-link-alerts.md) 同时运行飞端和地面端，测量双向连通、
时延、丢包、MTU、断链恢复和时钟偏差；如果设备只提供串口，再单独实现有界分帧适配。

定制 QGC 还需确认视频 Item 使用 `PreserveAspectFit` 还是 `PreserveAspectCrop`、视频实际
绘制矩形、Android 屏幕旋转和安全区域 inset。使用
`scripts/g20_viewport_mapping_demo.py` 的固定向量先验证 QML 变换，再连接触摸命令；屏幕
黑边中的触摸不能生成目标框。

GR01 签名元数据链路使用两端相同的应用 HMAC 密钥和 MAVLink2 签名密钥。密钥只通过
环境变量注入。先在 Jetson 端运行服务端：

```bash
python3 -m multidetect operator-udp-server \
  --bind-host 0.0.0.0 \
  --port 14580 \
  --operator-hmac-key-env MULTIDETECT_OPERATOR_HMAC \
  --mavlink-signing-key-hex-env MULTIDETECT_MAVLINK_SIGNING_KEY \
  --max-datagrams 300 \
  --exit-after-accepted-selections 100 \
  --receive-timeout-seconds 30
```

再从 GR01 地面侧可运行 Python 的测试机或厂商 SDK 测试环境发起：

```bash
python3 -m multidetect gr01-link-bench \
  --host JETSON_GR01_NETWORK_IP \
  --port 14580 \
  --operator-hmac-key-env MULTIDETECT_OPERATOR_HMAC \
  --mavlink-signing-key-hex-env MULTIDETECT_MAVLINK_SIGNING_KEY \
  --minimum-round-trips 100 \
  --retry-interval-seconds 0.5 \
  --maximum-attempts 3 \
  --maximum-packet-loss-rate 0.01 \
  --maximum-ack-latency-p95-ms 500 \
  --hardware-mode \
  --hardware-id REPLACE_WITH_GR01_SERIAL_OR_ASSET_ID \
  --out artifacts/evaluation/gr01-bench.json
```

`--hardware-mode` 会拒绝 localhost/回环地址。通过结果必须包含100次已接受往返、双向 IP、
应用 HMAC、MAVLink2 签名、丢包率不超过1%和 ACK P95不超过500 ms。本机直连不加
`--hardware-mode` 时只会生成 `gr01_software_baseline_bench_passed`，不能满足
`airframe_bench`。

## E. 第五批：独立载荷控制器HIL

第一步只提供状态，不接执行器：

- 控制器型号、MCU和固件版本。
- 唯一模块ID和协议版本。
- 舱位编号、载荷类型和存在传感器。
- 独立硬件互锁状态。
- 状态报告周期、单调序号、控制器时钟来源。
- HMAC密钥ID；密钥本体通过本地环境或密钥系统注入，不能提交到仓库。

先生成与 [`examples/payload_inventory.demo.json`](../examples/payload_inventory.demo.json) 同结构的只读报告，并运行：

```bash
python3 -m multidetect payload-inventory-check \
  configs/missions/fire_suppression.demo.json \
  artifacts/payload-controller-status.json \
  --now-s 1000.5 \
  --hmac-key-env MULTIDETECT_PAYLOAD_HMAC \
  --expected-key-id payload-key-v1
```

只有只读状态、认证、防重放、故障注入和惰性载荷台架验证完成后，才能另行评审真实非危险灭火载荷接口。

请求/反馈消息的任务、授权、目标、舱位、时间、序号、HMAC 和状态迁移约束已经在
[`payload-controller-hil-protocol.md`](payload-controller-hil-protocol.md) 中定义。下一步
需要控制器团队确认这些字段能否由固件持久化，并选择串口、CAN 或独立 IP 传输；在该选择
完成前，协议不会连接任务状态机或任何实体端口。

当控制器和独立传感器能够分别导出签名 JSONL 后，按
[`payload-controller-hil-protocol.md`](payload-controller-hil-protocol.md#惰性硬件台架只读验收)
运行 `inert-payload-bench-check`。它要求20次已执行+独立离舱确认，并额外要求一次不确定
结果且零自动重试的故障注入；工具只验证日志，不向控制器发送任何命令。通过结果才可作为
`inert_payload_bench` 的载荷记录，但仍不是实体灭火弹投放或现场运行批准。

## 最小交付包

要继续下一步，只需先提供：

```text
1. fire-smoke-nms.onnx
2. fire-smoke-nms.manifest.json
3. jetson-info.txt
4. RTSP摄像头型号、编码、分辨率、FPS和脱敏路径
```

Pixhawk、数传接收端和载荷控制器资料可以随后补充。
