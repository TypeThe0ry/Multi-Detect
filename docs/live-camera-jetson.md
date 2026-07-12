# 本机摄像头、RTSP、Jetson 与 Pixhawk 只读遥测

本页接入的是实时感知链路：

```text
本机摄像头 / RTSP
  -> OpenCV VideoCapture（单帧缓冲、断流重连）
  -> ONNX Runtime（严格 post-NMS Nx6）
  -> fire_smoke_legacy 适配器
  -> 跟踪、融合、安全规则、OpenCV 授权界面
```

`live-camera` 从不创建真实载荷释放、飞控、模式切换、任务上传、参数写入或 MAVLink 命令。Pixhawk 接口只读取
`HEARTBEAT`、`ATTITUDE`、`GLOBAL_POSITION_INT`、`SYS_STATUS`、`GPS_RAW_INT` 和 `MISSION_CURRENT`。围栏许可、允许部署模式、投放区净空等通用 MAVLink 无法可靠推断的条件保持 `None`，因此规则引擎会拒绝部署。

## Windows 本机摄像头

先安装本项目的可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[vision,pixhawk]"
```

探测摄像头（读取一帧后立即释放，不写图像文件）：

```powershell
.\.venv\Scripts\python.exe -m multidetect camera-check --source 0
```

连续采集冒烟测试（不保存画面）：

```powershell
.\.venv\Scripts\python.exe -m multidetect camera-check --source 0 --frames 120
```

结果会报告平均FPS、采集延迟P50/P95和重连次数。2026-07-12最新回归在当前开发机实测为640×480、120帧、约22.77 FPS、P95约48.55 ms、0次重连；该数字只代表本机摄像头，不代表Jetson/RTSP性能。

Windows 上 `--backend auto` 会优先使用 DirectShow；若设备仍无法抓帧，先关闭占用摄像头的应用，再显式重试：

```powershell
.\.venv\Scripts\python.exe -m multidetect camera-check --source 0 --backend dshow
```

运行实时火烟 ONNX 模型。模型必须已经包含 NMS，并返回 `N×6` 或 `1×N×6`：
`x1, y1, x2, y2, confidence, class_id`。原始 YOLO head 输出不能直接传入此入口。

拿到模型后先做接口和性能验收：

```powershell
.\.venv\Scripts\python.exe -m multidetect model-check --onnx-model models\fire-smoke-nms.onnx --model-manifest models\fire-smoke-nms.manifest.json --class-names fire,smoke --output-coordinates normalized_xyxy --provider CPUExecutionProvider
```

该命令输出模型SHA-256、实际启用的Provider、输入尺寸以及预热后的P50/P95推理延迟，并实际检查后NMS `Nx6` 输出。模型清单同时绑定实际ONNX的SHA-256、模型版本、类别顺序、严格Nx6字段、用途限制和批准状态。正式验证时增加 `--require-production-approved`；未提供清单、哈希不一致、类别顺序不一致或未批准都会失败。合成黑图测试不验证识别准确率，输出会明确标记 `accuracy_validated=false`。

零载荷巡检构型：

```powershell
.\.venv\Scripts\python.exe -m multidetect live-camera configs\missions\fire_patrol.demo.json --source 0 --onnx-model models\fire-smoke-nms.onnx --model-manifest models\fire-smoke-nms.manifest.json --class-names fire,smoke --output-coordinates normalized_xyxy
```

该构型确认火情后立即输出一条 JSON Lines 告警并继续搜索，不会创建授权挑战。实时窗口显示FPS、检测框、已确认 Track ID、跟踪持续时间、目标队列、火情横幅、事件记录、飞机坐标、只读飞行遥测和相对航迹图。按 `C` 可确认当前告警。告警里的 `aircraft_position` 是发现目标时的飞机位置，不是未经标定计算的火点坐标。
退出时还会输出采集和推理延迟的 P50/P95、平均 FPS 以及摄像头重连次数；统计窗口默认最多保留最近 600 帧，不会随着长航时巡逻无限增长。
态势台同时显示 `CAMERA OK`、重连次数和 `MODEL OK`。采集或推理抛出异常时，程序先写入 `camera.read_failed` 或 `perception.inference_failed` 审计事件，然后失败退出；不会把模型故障转换成“当前帧没有火灾”。

如需在数传适配器暂时失败或进程重启后保留未确认告警，启用SQLite发件箱：

```powershell
.\.venv\Scripts\python.exe -m multidetect live-camera configs\missions\fire_patrol.demo.json --source 0 --onnx-model models\fire-smoke-nms.onnx --alert-outbox artifacts\fire-alerts.sqlite3
```

告警会先持久化再发布；发布成功后标记完成，失败时保留为 `pending`，下次启动优先重试。当前JSON Lines写入成功只代表本地发布器已接受数据，不代表远端数传站已经确认；接真实链路时必须改用下面的ACK发布契约。

代码层已经提供关联 `alert_id` 的ACK校验、有限次数重试和指数退避，并用纯回环HIL传输验证；SQLite发件箱只有在收到匹配告警ID、有效接收端身份和有效ACK时间戳后才标记完成。真实电台/MAVLink传输、认证、抗重放和接收端实现仍未接入。

启动时会保留全部未确认告警，并只保留最近10000条已确认记录，防止长期服务的发件箱无限增长。删除的是已确认历史，不会清除待发送告警。

实时任务建议同时指定 `--audit-out artifacts/live-mission.audit.jsonl`。实时模式会逐事件写入，只在内存保留最近10000条事件；告警、授权、任务状态、操作员动作和载荷事件立即执行 `fsync`，高频普通感知事件按最多100条一批同步，以避免每帧磁盘同步拖垮实时推理。程序正常退出时还会执行最终同步。

实时审计采用追加模式，每次启动生成新的 `session_id`，服务重启不会覆盖上一轮记录。若断电留下不完整的最后一行，下次启动只截断该残缺尾行，保留之前已经完整写入的事件。

模型评估时增加 `--prediction-log-out artifacts/predictions.jsonl`，程序会为每个处理帧记录归一化检测框、类别、置信度、模型版本和推理延迟，不保存原始画面。准备具有相同 `frame_id` 的人工标注JSONL后运行：

```powershell
.\.venv\Scripts\python.exe -m multidetect evaluate-detections artifacts\ground-truth.jsonl artifacts\predictions.jsonl --iou-threshold 0.5 --confidence-threshold 0.25
```

输出包含逐类别和总体TP/FP/FN、precision、recall、误报帧数、漏报帧数以及推理延迟P50/P95。仓库中的 `examples/evaluation_*.demo.jsonl` 只验证计算器，不是模型质量证据。

带仿真载荷构型：

```powershell
.\.venv\Scripts\python.exe -m multidetect live-camera configs\missions\fire_suppression.demo.json --source 0 --onnx-model models\fire-smoke-nms.onnx --class-names fire,smoke --max-frames 300
```

窗口中的 `A` 仅记录人工批准并将任务推进到 `DEPLOYMENT_READY`；`D` 拒绝；`Q` 退出。没有键盘或网络路径能从该命令触发真实释放。

实时模式不会只凭任务配置相信载荷已经安装。没有独立载荷清单提供器时，界面显示 `INVENTORY UNVERIFIED`，仍可巡检，但不会创建载荷授权挑战。可先用只读JSON HIL报告核对协议、模块身份、舱位、类型、锁定状态和存在传感器：

```powershell
.\.venv\Scripts\python.exe -m multidetect payload-inventory-check configs\missions\fire_suppression.demo.json examples\payload_inventory.demo.json --now-s 1000.5
```

真实控制器报告不能使用未认证JSON。控制器应在报告中写入 `key_id`、单调递增 `sequence` 和 `signature_hmac_sha256`；密钥只通过环境变量交给Jetson，不写在命令行或报告中：

```powershell
$env:MULTIDETECT_PAYLOAD_HMAC = "由部署系统注入的密钥"
.\.venv\Scripts\python.exe -m multidetect payload-inventory-check configs\missions\fire_suppression.demo.json artifacts\payload-controller-status.json --now-s 1000.5 --hmac-key-env MULTIDETECT_PAYLOAD_HMAC --expected-key-id payload-key-v1
```

实时只读文件桥接使用 `--payload-inventory-report`、`--payload-inventory-hmac-key-env` 和 `--payload-inventory-key-id`。HMAC失败、密钥ID错误、时间戳过期、序号回退或清单变化都会保持或重新进入 `INVENTORY UNVERIFIED`。该文件桥接仍不包含载荷控制器写入或执行器接口。

软件/HIL演示需要完整走通授权后的仿真反馈时，必须显式增加 `--simulate-payload-cycle`。该参数同时显式启用与任务配置一致的仿真清单；授权成功后按 `S`，程序只会向内存中的 `FakePayloadPort` 提交请求，并模拟执行报告与独立舱位传感器确认：

```powershell
.\.venv\Scripts\python.exe -m multidetect live-camera configs\missions\fire_suppression.demo.json --source 0 --onnx-model models\fire-smoke-nms.onnx --simulate-payload-cycle
```

零载荷巡检配置会拒绝该参数。该开关没有GPIO、串口、CAN、MAVLink命令或真实执行器实现，不能用于物理载荷。

未来载荷控制器还必须先通过只读库存证据校验。当前HIL示例命令如下：

```powershell
.\.venv\Scripts\python.exe -m multidetect payload-inventory-check configs\missions\fire_suppression.demo.json examples\payload_inventory.demo.json --now-s 1000.5
```

校验覆盖协议版本、模块身份、舱位编号和类型、锁定状态、控制器/总互锁健康、载荷存在及独立存在传感器健康。文件型HIL提供器还支持HMAC-SHA256、key ID、单调序列、回退拒绝及同序列内容一致性。该命令只读取JSON报告，不发送释放指令；真实设备的安全密钥注入、轮换和传输保护仍需在硬件阶段设计。

零载荷巡检配置允许RGB火烟模型经多帧确认后发送“疑似火情”告警，但不会进入授权或载荷流程。载荷构型仍保持失败闭锁：仅有火烟模型时，`person_detector_healthy=false`，且热像、飞控安全条件未知，任务会停在监控/搜索状态。要让授权界面显示可评估候选，必须加入覆盖配置中全部 `person_labels` 的独立安全对象模型；即使如此，没有经过验证的围栏和投放区净空源，规则仍会拒绝。

## RTSP + Jetson Orin Nano

在真实模型到位前，可按根目录README生成恒定输出的 synthetic HIL ONNX，并且必须显式
使用 `--allow-synthetic-hil-model`。该制品只验证接口；它会为所有画面制造同一个火焰
候选，绝对不能用于准确率评估、现场巡检或Jetson服务。生产服务模板没有该开关，并
要求生产批准清单，因此会拒绝这类制品。

建议先使用 `camera-check` 验证网络流，再接 ONNX：

```bash
export CAMERA_SOURCE='rtsp://USER:PASSWORD@CAMERA_HOST:554/STREAM'
python3 -m multidetect camera-check \
  --source-env CAMERA_SOURCE \
  --rtsp-transport tcp

python3 -m multidetect live-camera configs/missions/fire_suppression.demo.json \
  --source-env CAMERA_SOURCE \
  --rtsp-transport tcp \
  --onnx-model models/fire-smoke-nms.onnx \
  --class-names fire,smoke \
  --input-width 640 --input-height 640 \
  --provider TensorrtExecutionProvider \
  --provider CUDAExecutionProvider \
  --provider CPUExecutionProvider \
  --trt-engine-cache artifacts/trt-engine-cache
```

在Jetson上也应先运行 `model-check`，确认 `active_providers` 的首选项确实是 `TensorrtExecutionProvider` 或经过批准的CUDA回退，而不是静默落到CPU。

使用 `--source-env` 时，进程参数只包含环境变量名，RTSP URI 不会出现在应用参数、应用错误或审计日志中。OpenCV/FFmpeg 自身日志属于外部边界，上机后仍须检查 systemd 日志是否会由底层库打印完整 URI。先用 TCP 取得稳定性；确认网络可承受丢帧后，再评估 UDP 的低延迟取舍。采集端请求 `CAP_PROP_BUFFERSIZE=1`，以丢弃旧帧而不是累积端到端延迟。
默认一次读帧失败后最多重连 3 次、间隔 0.25 秒。可通过 `--reconnect-attempts` 和 `--reconnect-delay-seconds` 调整；达到上限仍无画面时进程失败退出，而不是继续使用旧帧。

在 Jetson 上，使用与目标 JetPack、CUDA、TensorRT 对应的 NVIDIA 运行时，不要把 Windows/x86 的 ONNX Runtime 或 TensorRT engine 复制过去。若 Jetson 已由系统或 NVIDIA 容器提供兼容的 OpenCV、ONNX Runtime 和 `pymavlink`，以 `pip install -e . --no-deps` 安装项目，避免 `.[vision]` 替换平台运行时。TensorRT engine 应在目标 Orin Nano 或完全兼容的构建环境生成、测基线并记录哈希。`--trt-engine-cache` 仅启用 ORT 的本地 engine cache，缓存目录仍必须随着 JetPack/CUDA/TensorRT 或模型哈希变化而失效重建。NVIDIA 说明 ONNX Runtime 的 TensorRT Execution Provider 在 TensorRT 11.x 需要 ONNX Runtime 1.27 或更高版本；引擎/运行时迁移也必须按 JetPack/CUDA 组合验证。

## Pixhawk V6X：只读 MAVLink 遥测

Pixhawk V6X 的 `TELEM` 端口可连接 MAVLink/遥测设备。将 Jetson 串口或网络桥接为只读输入后，例如：

```bash
python3 -m multidetect live-camera configs/missions/fire_suppression.demo.json \
  --source-env CAMERA_SOURCE \
  --onnx-model models/fire-smoke-nms.onnx \
  --class-names fire,smoke \
  --pixhawk-endpoint /dev/ttyTHS1 \
  --pixhawk-baud 57600
```

也可使用现有网络 MAVLink bridge：`--pixhawk-endpoint udp:127.0.0.1:14550`。

接真实巡航任务时，不应使用默认的“立即模拟已到任务区”生命周期。启用只读飞控观察，并填写任务航线中代表进入搜索区的序号：

```bash
python3 -m multidetect live-camera configs/missions/fire_patrol.demo.json \
  --source-env CAMERA_SOURCE \
  --onnx-model models/fire-smoke-nms.onnx \
  --pixhawk-endpoint /dev/ttyTHS1 --pixhawk-baud 57600 \
  --observe-pixhawk-lifecycle \
  --task-area-mission-sequence 3 \
  --allowed-auto-mode AUTO_MISSION
```

程序只有在链路和定位新鲜、飞机已解锁、飞行模式位于允许列表且 `MISSION_CURRENT.seq` 达到配置值后，才把任务推进到搜索状态。它不会发送解锁、起飞、模式切换、任务上传、航点跳转或返航命令。

接摄像头和模型之前，可单独验证V6X只读链路：

```bash
python3 -m multidetect pixhawk-check \
  --endpoint /dev/ttyTHS1 \
  --baud 57600 \
  --samples 20 \
  --interval-seconds 0.2 \
  --require-fresh-link
```

该命令只调用非阻塞接收，汇总心跳/位置新鲜样本和最后一份遥测，并明确输出 `messages_transmitted=0`。如果启用 `--require-fresh-link` 且采样窗口内没有新鲜心跳，命令以失败结束。

当前实现只读取姿态、相对高度、经纬度、航向、水平速度、电池、卫星数、解锁状态、飞行模式、任务序号、链路和定位新鲜度。它不发送 heartbeat、参数请求、命令、任务、actuator、stream-rate 或模式切换消息。请先以 PX4/QGroundControl 配置正确的 TELEM 端口和波特率，并在断电状态下确认线序、电平、供电和接地；本项目不替代飞控接线、法规或安全评审。

## Jetson 优化验收顺序

1. 在录像回放上验证 ONNX 模型确实是 post-NMS Nx6，逐框比较类别、置信度和坐标。
2. 在 Orin Nano 上记录 CPU/GPU、JetPack、CUDA、TensorRT、ONNX Runtime provider、模型和 engine 哈希。
3. 测量 RTSP 解码到显示的 P50/P95 延迟、断流重连、丢帧率、GPU/内存和热降频。
4. 单独接入人员/消防员/车辆/建筑等安全模型，验证 `person_detector_healthy` 与漏检降级。
5. 将 Pixhawk 遥测先作为只读监控，与 QGroundControl/飞控日志逐字段核对。
6. 只有完成持久化审计、地理围栏/投放区安全源、独立硬件互锁和受控 SIL/HIL 后，才另行设计真实执行器接口；本仓库当前没有此接口。
