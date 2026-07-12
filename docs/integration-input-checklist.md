# 集成输入清单

按以下顺序准备即可，不需要一次提供全部硬件。密码、HMAC密钥、私钥和完整RTSP凭据不要粘贴到聊天、Git或审计文件中。

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

## 最小交付包

要继续下一步，只需先提供：

```text
1. fire-smoke-nms.onnx
2. fire-smoke-nms.manifest.json
3. jetson-info.txt
4. RTSP摄像头型号、编码、分辨率、FPS和脱敏路径
```

Pixhawk、数传接收端和载荷控制器资料可以随后补充。
