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

显示窗口只有摄像头画面和必要叠加信息。左键拖拽框选目标，再次拖拽切换目标；右键或
`X` 取消锁定，`Q` 退出。绿色粗框表示当前人工框选后持续关联的目标。这个操作只改变
视觉跟踪对象，不是载荷授权，也不会生成飞控或释放命令。

火烟候选默认采用保守的分级阈值（`flame=0.72`、`smoke=0.60`）并要求连续6帧空间一致。
任务确认仍使用任务配置中的 `minimum_confidence=0.82`，候选显示阈值不构成部署授权。
运行时还会过滤缺少火焰颜色纹理的白色高亮灯斑/反光；提供独立人员模型时，人员框
覆盖的火烟候选会被否决。以上仅用于降低候选误报，不能替代真实部署域负样本训练，
也不能把未经清单批准的通用人员模型当作载荷安全证据。

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

在进入实时推理前，可先对本机摄像头做无画面台架采集：

```powershell
.\.venv\Scripts\python.exe -m multidetect camera-bench `
  --source 0 `
  --minimum-frames 30 `
  --minimum-duration-seconds 1 `
  --maximum-duration-seconds 10 `
  --out artifacts\evaluation\local-camera-bench-short.json
```

RTSP/Jetson 验收应改用 `--source-env` 注入 URI，并保持默认的 300 帧、60 秒最低门槛。
本机摄像头结果只验证 OpenCV 采集路径，不会被证据检查器当作 RTSP 硬件结果。

Jetson 上的最终视觉台架还需运行 `jetson-vision-bench`。它同时执行真实采集和模型推理，
默认要求至少1000帧和1800秒，且必须识别到 Orin Nano、TensorRT/CUDA provider 与有效温度；
输出 `jetson_orin_nano_bench_passed` 后才可作为 `vision_bench` 的 Jetson 记录。完整命令见
[`integration-input-checklist.md`](integration-input-checklist.md#b-第二批jetson-orin-nano)。

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

无显示器的自动化软件验收可以再增加 `--auto-simulate-payload-cycle`。它只有在同一进程已
显式启用 `--simulate-payload-cycle`、任务完成安全检查且本地/G20 有效授权已经把状态推进
到 `DEPLOYMENT_READY` 后，才自动执行一次仿真周期：

```powershell
.\.venv\Scripts\python.exe -m multidetect live-camera `
  configs\missions\fire_suppression.demo.json `
  --source 0 --onnx-model models\fire-smoke-nms.onnx `
  --simulate-payload-cycle --auto-simulate-payload-cycle --no-display
```

这不是“检测到火就自动投放”：没有授权、授权过期、安全证据变化、库存未知或任务未到
`DEPLOYMENT_READY` 时不会执行。默认 Jetson 服务不包含两个仿真开关。真实载荷接入也
不能复用 FakePayloadPort 自动路径，必须经过下述双通道惰性 HIL 和独立安全评审。

零载荷巡检配置会拒绝该参数。该开关没有GPIO、串口、CAN、MAVLink命令或真实执行器实现，不能用于物理载荷。

需要把实时操作员按键接到完整的双通道惰性 HIL 时，还必须同时增加
`--inert-payload-hil`，并完整提供控制器请求、控制器反馈、独立传感器确认三组不同的
环境变量密钥和 key ID。当前实现故意只允许 localhost；它用于同一台 Jetson 上的拆桨
台架进程，不能冒充已完成的外部控制器硬件链路。示例参数结构如下：

```powershell
$env:HIL_REQUEST_KEY = "至少32字节且仅用于请求的随机密钥"
$env:HIL_RESULT_KEY = "至少32字节且仅用于控制器反馈的另一密钥"
$env:HIL_CONFIRM_KEY = "至少32字节且仅用于独立传感器的第三把密钥"
.\.venv\Scripts\python.exe -m multidetect live-camera `
  configs\missions\fire_suppression_fixed_wing.demo.json `
  --source 0 --onnx-model models\fire-smoke-nms.onnx `
  --simulate-payload-cycle --auto-simulate-payload-cycle --inert-payload-hil `
  --payload-hil-controller-port 15001 `
  --payload-hil-controller-module-id inert-controller-1 `
  --payload-hil-request-key-env HIL_REQUEST_KEY --payload-hil-request-key-id request-v1 `
  --payload-hil-result-key-env HIL_RESULT_KEY --payload-hil-result-key-id result-v1 `
  --payload-confirmation-port 15002 `
  --payload-confirmation-key-env HIL_CONFIRM_KEY --payload-confirmation-key-id confirm-v1 `
  --payload-confirmation-sensor-id bay-departure-sensor-1
```

使用自动开关时，有效授权进入 `DEPLOYMENT_READY` 后执行；删除自动开关则仍需按 `S`。
控制器拒绝、失败或通信超时立即进入闭锁故障；控制器报告 `EXECUTED` 后仍
等待第二个 UDP 端口的认证离舱证据。无效确认包会被丢弃，确认等待超时标记为不确定释放，
不会自动重试。先运行 `scripts\payload_mission_hil_loopback_demo.py` 验证完整契约，再启动
实时模式。

未来载荷控制器还必须先通过只读库存证据校验。当前HIL示例命令如下：

```powershell
.\.venv\Scripts\python.exe -m multidetect payload-inventory-check configs\missions\fire_suppression.demo.json examples\payload_inventory.demo.json --now-s 1000.5
```

校验覆盖协议版本、模块身份、舱位编号和类型、锁定状态、控制器/总互锁健康、载荷存在及独立存在传感器健康。文件型HIL提供器还支持HMAC-SHA256、key ID、单调序列、回退拒绝及同序列内容一致性。该命令只读取JSON报告，不发送释放指令；真实设备的安全密钥注入、轮换和传输保护仍需在硬件阶段设计。

零载荷巡检配置允许RGB火烟模型经多帧确认后发送“疑似火情”告警，但不会进入授权或载荷流程。载荷构型仍保持失败闭锁：仅有火烟模型时，`person_detector_healthy=false`，且热像、飞控安全条件未知，任务会停在监控/搜索状态。要让授权界面显示可评估候选，必须加入覆盖配置中全部 `person_labels` 的独立安全对象模型；即使如此，没有经过验证的围栏和投放区净空源，规则仍会拒绝。

安全对象模型不仅要覆盖 `person_labels`，还必须有通过哈希、类别、坐标和
`model_role=safety_object_evidence` 校验的清单；否则即使推理成功，运行时仍把
`person_detector_healthy` 置为 false。两个模型的全部清单门禁会在创建任一 ONNX
Runtime 会话之前完成。

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

V10 之后不再重复同一批背景。安装到固定翼前后，应在**人工确认全程无真实火烟**的
条件下，从真实 RTSP 视角采集云层、夕阳、道路、建筑反光、人员、车辆和地面热色物体。
URI 仍只从环境变量读取，每次使用全新的输出目录和唯一 session ID：

```bash
python3 scripts/mine_camera_hard_negatives.py \
  --onnx-model artifacts/training/hardneg-snapshots/v5-local-calibrated/best.onnx \
  --source-env CAMERA_SOURCE --rtsp-transport tcp \
  --session-id fixed-wing-bench-no-fire-001 \
  --scene-notes 'daylight, people, vehicles, reflective roofs' \
  --frames 18000 --sample-every 300 --trigger-spacing 15 --max-saved 1200 \
  --out artifacts/hard-negative-mining/fixed-wing-bench-no-fire-001 \
  --confirm-no-fire
```

工具会保存触发帧和周期抽样帧、空 YOLO 标签、逐帧候选元数据及
`session-manifest.json`。清单记录模型 SHA-256、推理提供器、分辨率、重连次数和场景说明，
但不记录 RTSP URI。所有图片仍标记为 `images_require_manual_review_before_training=true`；
必须人工剔除任何真实火烟、重复帧、隐私不合规画面和不确定样本后才能进入下一轮训练。

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
Pixhawk 底层提供器仍不自行声明“允许部署”；只有启用只读生命周期观察后，Live 任务层
才把新鲜链路、`armed=true` 和配置允许的飞行模式组合为
`flight_mode_allows_deploy=true`。未解锁、模式不允许或链路失效得到 false，任一值未知则
保持 unknown。定位、围栏、允许区域和投放区仍由各自独立规则检查，不能被 AUTO 模式
替代。

软件组合验收可执行：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_live.py::test_live_runner_closes_pixhawk_remote_authorization_and_two_channel_hil -q
```

该测试在一次 Live 运行中使用真实 localhost MAVLink/UDP、G20 双层认证 UDP 和两条载荷
HIL UDP 通道，验证 Pixhawk 接收端发送消息数为 0。摄像头、检测结果、无线电、V6X 和
载荷硬件仍为模拟，证据记录在
`artifacts/evaluation/combined-flight-stack-software-hil-acceptance.json`。

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

需要生成 `airframe_bench` 可用的 Pixhawk 证据时，使用 `pixhawk-v6x-bench` 和当前 QGC
静止台架快照。该命令至少采集100份新鲜样本，逐字段比较 QGC，并通过不再接收数据的
缓存快照验证 stale 后失败闭锁；完整格式和命令见
[`integration-input-checklist.md`](integration-input-checklist.md#c-第三批pixhawk-v6x只读接入)。

在没有 V6X 时，可以先用两个终端验证真实 pymavlink 编码、UDP、字段映射和新鲜度逻辑。
该发送器只产生 HEARTBEAT、ATTITUDE、GLOBAL_POSITION_INT、SYS_STATUS、GPS_RAW_INT 和
MISSION_CURRENT 仿真遥测，不发送 COMMAND、MISSION_ITEM、参数、模式切换或执行器消息：

```powershell
# 终端 1：仿真固定翼遥测源
.\.venv\Scripts\python.exe scripts\pixhawk_readonly_hil_sender.py `
  --endpoint udpout:127.0.0.1:14651 `
  --duration-seconds 10 --rate-hz 10

# 终端 2：现有 Jetson/Pixhawk 只读接收路径
.\.venv\Scripts\python.exe -m multidetect pixhawk-check `
  --endpoint udpin:127.0.0.1:14651 `
  --samples 50 --interval-seconds 0.1 --require-fresh-link
```

本机回环已验证经纬度、高度、姿态、航向、速度、电池、卫星数、解锁状态、AUTO 模式和
任务序号；接收器输出 `messages_transmitted=0`。围栏、允许区域和投放区仍保持 `null`，
证明通用遥测不会被误当成部署许可。此检查不能替代 ArduPlane SITL 或真实 V6X/QGC
逐字段对比。

当前实现只读取姿态、相对高度、经纬度、航向、水平速度、电池、卫星数、解锁状态、飞行模式、任务序号、链路和定位新鲜度。它不发送 heartbeat、参数请求、命令、任务、actuator、stream-rate 或模式切换消息。请先以 PX4/QGroundControl 配置正确的 TELEM 端口和波特率，并在断电状态下确认线序、电平、供电和接地；本项目不替代飞控接线、法规或安全评审。

### 独立区域安全证据（仅文件型 HIL）

Pixhawk 的 GPS 和通用 MAVLink 遥测不会被推断成“允许投放”。载荷构型在缺少
`in_allowed_zone`、`geofence_healthy` 或 `release_zone_clear` 任一独立证据时继续失败闭锁。
当前提供一个只读文件桥接，用于在 Jetson 台架上验证第二条证据链；它不修改 Pixhawk，
也没有 GPIO、串口、CAN 或载荷执行器写入功能。

Jetson 侧受信任的区域监控桥应持续原子更新如下 JSON；三个布尔值可以为 `false`，此时
代表经过认证的明确拒绝，而不是通信故障：

```json
{
  "protocol_version": 1,
  "observed_at_s": 12345.25,
  "source_id": "independent-zone-monitor",
  "mission_id": "fire-fixed-wing-hil-001",
  "sequence": 42,
  "key_id": "zone-key-v1",
  "latitude_deg": 31.123456,
  "longitude_deg": 121.654321,
  "in_allowed_zone": true,
  "geofence_healthy": true,
  "release_zone_clear": false,
  "signature_hmac_sha256": "由不含本字段的规范化 JSON 计算的 HMAC-SHA256"
}
```

实时命令增加以下参数：

```bash
export MULTIDETECT_ZONE_HMAC='由部署系统注入且不少于32字节的密钥'
python3 -m multidetect live-camera configs/missions/fire_suppression_fixed_wing.demo.json \
  --source-env CAMERA_SOURCE \
  --onnx-model models/fire-smoke-nms.onnx \
  --pixhawk-endpoint /dev/ttyTHS1 --pixhawk-baud 57600 \
  --zone-evidence-report /run/multi-detect/zone-evidence.json \
  --zone-evidence-hmac-key-env MULTIDETECT_ZONE_HMAC \
  --zone-evidence-key-id zone-key-v1 \
  --zone-evidence-max-position-delta-m 25
```

报告同时绑定任务 ID、Pixhawk 实时位置、任务配置中的证据最大年龄、key ID 和单调递增
序号。HMAC 错误、任务不符、位置不健康、经纬度偏差过大、时间戳过期、序号回退、同序号
内容变化或文件读取失败，都会把三个区域条件恢复为未知，从而禁止授权。文件型 HIL 使用
Jetson 主机的单调时钟秒数；报告应由同一主机上的受信任桥接进程写入。跨设备硬件源需要
另行设计经过验证的时钟映射、密钥注入、原子传输与故障检测，不能直接复用另一个设备的
开机计时值。

## Jetson 优化验收顺序

1. 在录像回放上验证 ONNX 模型确实是 post-NMS Nx6，逐框比较类别、置信度和坐标。
2. 在 Orin Nano 上记录 CPU/GPU、JetPack、CUDA、TensorRT、ONNX Runtime provider、模型和 engine 哈希。
3. 测量 RTSP 解码到显示的 P50/P95 延迟、断流重连、丢帧率、GPU/内存和热降频。
4. 单独接入人员/消防员/车辆/建筑等安全模型，验证 `person_detector_healthy` 与漏检降级。
5. 将 Pixhawk 遥测先作为只读监控，与 QGroundControl/飞控日志逐字段核对。
6. 先用签名文件 HIL 验证地理围栏/投放区证据的任务、位置、时间和重放绑定，再替换为经过独立验证的真实安全源。
7. 只有完成持久化审计、真实地理围栏/投放区安全源、独立硬件互锁和受控 SIL/HIL 后，才另行设计真实执行器接口；本仓库当前没有此接口。
