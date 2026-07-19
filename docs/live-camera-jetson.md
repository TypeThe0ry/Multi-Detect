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

本机框选与 G20 远程框选采用相同的影子跟踪策略：YOLO 已关联到火情轨迹时，用较紧的
检测框初始化 OpenCV 单目标跟踪器并在后台持续更新；YOLO 短时漏检时显示立即切到影子框，
同时仅把它作为检测器重捕获提示。影子框不会进入火灾确认、安全规则、授权或载荷输入。

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
默认要求至少 54000 帧和 3600 秒、处理速率不低于 15 FPS、推理 P95 不超过 66.7 ms，
捕获队列高水位不超过 1，且必须识别到 Orin NX/Nano、TensorRT/CUDA provider 与有效温度；
60 秒预热后还会按秒采样进程 RSS，要求首尾稳健增长不超过 256 MB，并报告每小时趋势；
输出 `jetson_orin_bench_passed` 后才可作为 `vision_bench` 的 Jetson 记录。完整命令见
[`integration-input-checklist.md`](integration-input-checklist.md#b-第二批jetson-orin-nxnano)。

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

实时任务建议同时指定 `--audit-out artifacts/live-mission.audit.jsonl`。任务控制器即使没有
输出文件也只在内存保留最近10000条事件；指定输出后则逐事件持久化完整审计流，同时仍只
保留最近10000条在内存。告警、授权、任务状态、操作员动作和载荷事件立即执行 `fsync`，
高频普通感知事件按最多100条一批同步，以避免每帧磁盘同步拖垮实时推理。程序正常退出时
还会执行最终同步。

实时审计采用追加模式，每次启动生成新的 `session_id`，服务重启不会覆盖上一轮记录。若断电留下不完整的最后一行，下次启动只截断该残缺尾行，保留之前已经完整写入的事件。

模型评估时增加 `--prediction-log-out artifacts/predictions.jsonl`，程序会为每个处理帧记录归一化检测框、类别、置信度、模型版本和推理延迟，不保存原始画面。启用统一目标池后必须同时增加 `--identity-tracking-log-out artifacts/identity-tracks.jsonl` 和 `--identity-tracking-session-id <UUID>`，会记录逐帧 `track_id`、状态、轨迹框及证据会话号，用于 IDF1、遮挡恢复和出画重捕获评测。录像端必须使用同一 UUID；缺失或不同会话会被复核包生成器拒绝。准备具有相同 `frame_id` 的人工标注JSONL后运行：

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

零载荷巡检配置允许 RGB 火烟模型经多帧确认后发送“疑似火情”告警，但不会进入授权或
载荷流程。当前硬件没有热成像，现行任务配置也不要求热像。载荷构型仍保持失败闭锁：仅有
主火烟模型时，`person_detector_healthy=false` 且
`independent_rgb_corroborated=false`，飞控安全条件也可能未知，任务会停在监控/搜索状态。
要让授权界面显示可评估候选，必须加入覆盖配置中全部 `person_labels` 的独立安全对象模型，
并加入与主检测器独立验收的 RGB 火情复核模型；即使如此，没有经过验证的围栏和投放区净空
源，规则仍会拒绝。

安全对象模型不仅要覆盖 `person_labels`，还必须有通过哈希、类别、坐标和
`model_role=safety_object_evidence` 校验的清单；否则即使推理成功，运行时仍把
`person_detector_healthy` 置为 false。两个模型的全部清单门禁会在创建任一 ONNX
Runtime 会话之前完成。

## RTSP + Jetson Orin NX/Nano

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

使用 `--source-env` 时，进程参数只包含环境变量名，RTSP URI 不会出现在应用参数、应用错误或审计日志中。OpenCV/FFmpeg 自身日志属于外部边界，上机后仍须检查 systemd 日志是否会由底层库打印完整 URI。先用 TCP 取得稳定性；确认网络可承受丢帧后，再评估 UDP 的低延迟取舍。采集端请求 `CAP_PROP_BUFFERSIZE=1` 以限制后端缓存；应用层默认另有 4 帧有界 FIFO，由独立采集线程按顺序交付且不主动跳帧。队列满时采集线程背压等待，并在性能摘要中报告高水位和背压次数。可用 `--capture-queue-frames` 调整容量，设为 0 才禁用应用层队列。该保证只覆盖应用层，不能把摄像头编码器、网络或解码器已丢失的帧宣称为已捕获。
默认一次读帧失败后最多重连 3 次、间隔 0.25 秒。可通过 `--reconnect-attempts` 和 `--reconnect-delay-seconds` 调整；达到上限仍无画面时进程失败退出，而不是继续使用旧帧。

当前 Jetson Orin NX 台架在 2026-07-13 使用网口三体摄像头的 720P/H.265 RTSP、
TensorRT 8.6 FP16 engine 和 4 帧 FIFO 完成 250 帧连续运行：稳态采集 25.40 FPS、
稳态处理 25.43 FPS、推理 P50/P95 为 29.80/30.32 ms、0 次重连、队列高水位 4、
0 次背压。启动到首帧 1.46 秒；总平均 22.10 FPS 包含模型预热和首帧启动时间。
该次室内画面没有检测候选，只证明链路和实时性能，不证明火灾识别准确率。当前 engine
仍处于隔离状态，未通过准确率批准，不能用于生产告警或物理载荷控制。

2026-07-14 又完成了同一实机链路的完整30分钟浸泡：处理44,967帧，摄像头重连、采集失败和
推理失败均为0；TensorRT 推理 P50/P95 为29.54/29.90 ms，最高温度52.72°C。证据位于
`artifacts/evaluation/jetson-orin-soak-20260714T0014.json`。该次诊断把 detector floor
降到0.10以观察模型原始输出，不等于任务报警阈值：41.70%的帧出现原始候选，其中
`flame` 置信度 P50/P95 为0.146/0.219，最大0.71875，仍低于任务层0.72；`smoke` 仅3个且
最大0.130。候选主要是约画面0.1%的小框。当前画面未经人工确认和逐帧标注，所以这些数据
只能说明运行时分布，不能自动认定为负样本或据此单独调整阈值。模型仍为
`quarantined`、`production_approved=false`，测试期间飞控和物理载荷控制均为关闭状态。

相机手册中的 `Led灯` 是 6 W 双远光夜视补光灯，可在厂商 Skydroid Camera FPV App
中切换。2026-07-13 台架曾向相机发送从厂商 App 静态分析得到的开/关指令并在结束路径
强制发送关闭，但日间三组 RTSP 画面亮度无可确认变化，因此自动控制路径仍记为
**未验证**。在 G20 上用厂商 App 现场观察灯体开关，或抓取该次控制流量确认传输封装前，
不得把该指令接入常驻服务。

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

## Pixhawk V6X：MAVLink 遥测与 Mode 3 姿态控制

本机使用 Holybro Pixhawk Jetson Baseboard。载板框图显示 Jetson 与 V6X 之间并行提供
内部 Ethernet、`UART1 <-> TELEM2` 和 CAN，不需要外接 Jetson-飞控数据线。2026-07-13
实机已确认当前 V6X 从 `192.168.0.3:14550` 向 `192.168.0.255:14550` 广播 MAVLink 2；
Jetson 临时加入 `192.168.0.1/24` 后，项目只读适配器 30 次采样取得 25 次新鲜心跳，
并保持 `messages_transmitted=0`。因此当前主链路采用内部 Ethernet，UART 只保留为后续冗余。
随后固化第二地址并复测 50 次得到 49 次新鲜心跳。2026-07-14 再次进行 15 秒原始被动
监听，从 `192.168.0.3:14550` 收到 71 个 MAVLink 2 帧和 15 个心跳；项目自身严格检查的
30 次采样取得 27 次新鲜底层心跳。当前心跳已正确声明 `PX4 + MAV_TYPE_FIXED_WING`，证明
板载通信与机型身份均已建立；但系统状态仍为 `MAV_STATE_UNINIT`，且尚无新鲜全局位置，
因此资格链路按设计保持拒绝。这不代表飞控已完成传感器、GPS、电源或安全配置。

Jetson 的 `eth0` 同时保留相机网络 `192.168.144.20/24` 和板内飞控网络
`192.168.0.1/24`。PX4 发往子网广播地址，所以接收器必须监听通配地址：

```bash
python3 -m multidetect live-camera configs/missions/fire_suppression.demo.json \
  --source-env CAMERA_SOURCE \
  --onnx-model models/fire-smoke-nms.onnx \
  --class-names fire,smoke \
  --pixhawk-endpoint udp:0.0.0.0:14550 \
  --pixhawk-baud 921600 \
  --pixhawk-system-id 1 \
  --pixhawk-expected-autopilot px4 \
  --pixhawk-expected-vehicle-type fixed_wing \
  --require-pixhawk-operational-state
```

`--pixhawk-baud` 对 UDP 不生效，只为统一 CLI 及 `/dev/ttyTHS1:921600` 后备链路保留。

### Mode 3 固定翼真实控制入口

`src/multidetect/fixed_wing_aim_control.py` 提供独立的写入 provider，不改变
`PixhawkReadOnlyTelemetryProvider` 的语义。Mode 3 只有在主 LCK、目标绑定确认、PX4 固定翼身份、
飞控 `armed=true`、链路/姿态/目标新鲜、最低空速与最低 AGL 全部成立时才发送
`SET_ATTITUDE_TARGET`。进入控制前连续预发送 setpoint，随后请求 `OFFBOARD`；失锁、取消、过期或
任一飞行门限失效时停止 setpoint 并恢复进入前的飞行模式；入口模式缺失时才回退 `AUTO`。yaw 固定为本次 LCK 激活时的航向，roll/pitch
只做绝对值和 slew 双重限幅，因此不会把每帧航向漂移累积成大范围改向。

写入 provider 同时请求 20 Hz `RC_CHANNELS`，并在每次执行确认建立 18 通道 PWM 基线。任一通道
相对基线变化达到 50 μs 会在下一条 RC 消息到达时取消瞄准、停止 setpoint、恢复进入前模式，
并通过签名状态包通知 QGC。RC 样本缺失或超过 0.30 秒时，控制器保持 inhibited。QGC 对实际
PRESTREAM/ACTIVE/REACQUIRING 状态显示红色脉冲横幅和大号取消按钮；遥控接管后保留醒目取消提示。

Jetson launcher 的生产开关如下；默认巡检不带该开关：

```bash
export OPERATOR_UDP_ENABLED=1
export UNIFIED_TARGET_POOL_ENABLED=1
export MONOCULAR_AVOIDANCE_ENABLED=1
export MULTIMODAL_RANGING_ENABLED=1
export RANGING_CALIBRATION_PATH=/home/jetson/Multi-Detect/artifacts/calibration/camera-main-v1.json
export MODE3_CONFIRMATION_ENABLED=1
export MODE3_AIM_CONTROL_ENABLED=1
./scripts/run_jetson_fire_patrol.sh
```

`MODE3_CONFIRMATION_ENABLED=1` 加入 `--mode3-aim` 并保持 QGC 签名确认链在线；
`MODE3_AIM_CONTROL_ENABLED=1` 再加入 `--fixed-wing-aim-control`，默认限制为
`roll ±20°`、`pitch ±15°`、单次修正 `roll ±10°/pitch ±6°`、slew `35°/s` 和 `25°/s`。
RC 中断默认参数可通过 `AIM_RC_INPUT_RATE_HZ`、`AIM_RC_INPUT_MAXIMUM_AGE_SECONDS` 和
`AIM_RC_CANCEL_THRESHOLD_US` 调整。
物理释放接口不在这条控制链中。

接真实巡航任务时，不应使用默认的“立即模拟已到任务区”生命周期。启用只读飞控观察，并填写任务航线中代表进入搜索区的序号：

```bash
python3 -m multidetect live-camera configs/missions/fire_patrol.demo.json \
  --source-env CAMERA_SOURCE \
  --onnx-model models/fire-smoke-nms.onnx \
  --pixhawk-endpoint udp:0.0.0.0:14550 --pixhawk-baud 921600 \
  --pixhawk-system-id 1 --pixhawk-expected-autopilot px4 \
  --pixhawk-expected-vehicle-type fixed_wing --require-pixhawk-operational-state \
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
  --endpoint udp:0.0.0.0:14550 \
  --baud 921600 \
  --samples 50 \
  --interval-seconds 0.2 \
  --expected-system-id 1 \
  --expected-autopilot px4 \
  --expected-vehicle-type fixed_wing \
  --require-operational-state \
  --require-fresh-link \
  --require-fresh-position
```

该命令只调用非阻塞接收，汇总心跳/位置新鲜样本、飞控身份和最后一份遥测，并明确输出
`messages_transmitted=0`。底层心跳与资格后的 `link_healthy` 分开统计；因此当前即使持续
收到 V6X 广播，也会因 `UNINIT + 无全局位置` 返回结构化失败，而不是把“能通”误报为
“可飞”。2026-07-14 的当前实机证据保存在
`artifacts/evaluation/v6x-jetson-readonly-link-20260714T025540Z.json`。

2026-07-13 的 RTSP + TensorRT + V6X 同进程实机复测连续处理 250 帧：稳态采集
25.26 FPS、稳态处理 25.53 FPS、推理 P50/P95 为 29.99/30.54 ms、摄像头 0 次重连。
室内画面 250 帧均无火烟候选，最终任务阶段保持 `standby`，授权、告警投递和模拟载荷
动作均为 0。只读飞控诊断共接收 41 条 MAVLink 消息、发送 0 条；当时原始心跳新鲜，但
资格链路为 false，原因是机型仍为 `MAV_TYPE_GENERIC`、状态仍为 `MAV_STATE_UNINIT`，同时
没有全局位置。2026-07-14 的单独链路复测已经观察到固定翼身份，但 `UNINIT` 和无全局位置
仍未解除。生命周期审计明确记录链路未通过资格、位置不健康、未解锁和模式不允许四项
闭锁原因。联合 RTSP 证据保存在
`artifacts/deployment/jetson-bench/combined_rtsp_v6x_20260713T164401.audit.jsonl` 和同名
`.predictions.jsonl`。每次带 Pixhawk 的 Live 运行结束时都会写入
`live.pixhawk_read_only_summary`，把原始链路、资格链路、心跳身份和收发计数放在同一份
审计证据中。

参数备份与上述零发送通道严格分离。新命令 `pixhawk-param-backup` 必须显式提供
`--acknowledge-active-read-request`，只发送一条 `PARAM_REQUEST_LIST`，并将参数解码值、
原始4字节、类型、索引和列表哈希原子写入新文件；它没有参数写入、飞行命令、任务或
执行器发送代码。PX4 必须指定 `--parameter-encoding bytewise`，防止整数值被错误解释为
普通浮点数。localhost 真实 pymavlink UDP HIL 已验证三项参数完整回传、PX4 hash 元数据、
主动读取1条以及参数写入/飞行命令/任务/执行器消息全部为0，证据在
`artifacts/evaluation/pixhawk-param-backup-localhost-hil.json`。

2026-07-14 经 GR01 `tcp:192.168.144.11:5760` 对真实 V6X 执行了一次授权的只读列表请求：
1102/1102 项完整回传，PX4 参数哈希为 `a67025ae`，规范化列表 SHA-256 为
`7a81c46f4aa3e365cdd9e217fffb1d7ab0168e3592733c0007eaab2e4a98daa0`，来源拒绝和无效消息
均为0；只发送1条 `PARAM_REQUEST_LIST`，参数写入、飞行命令、任务和执行器消息均为0。
快照和离线自洽校验分别保存在
`artifacts/evaluation/v6x-parameters-readonly-20260714.json` 与同名 `.verify.json`。实测
`SER_TEL1_BAUD=115200`、`COM_DL_LOSS_T=10`、`NAV_DLL_ACT=0`；本次没有修改参数。
当前 QGC 元数据把 `COM_DL_LOSS_T` 的有效范围定义为5至300秒，并把 `NAV_DLL_ACT=0/1/2/3/5/6`
分别定义为禁用/保持/返航/降落/终止/解除武装。失链动作必须结合固定翼现场方案单独确认，
不能把数值1误写成1秒超时。完整命令见
[`integration-input-checklist.md`](integration-input-checklist.md#c1-明确授权后的参数备份)。
`pixhawk-param-verify` 和 `pixhawk-param-diff` 随后在离线状态验证快照并对白名单外的任意
配置变化失败闭锁；二者发送消息数为0。内嵌列表哈希只用于自洽检查，不冒充数字签名。

同一份已验证快照现在还能离线审计三条互相独立的链路：

```bash
python3 -m multidetect pixhawk-link-audit \
  artifacts/evaluation/v6x-parameters-readonly-20260714.json \
  --out artifacts/evaluation/v6x-link-topology-audit-20260714.json
```

实机快照的结果是：GR01↔V6X 使用 `MAV_0/TELEM1/115200`；Jetson↔V6X 主链路使用
`MAV_2/Ethernet/UDP 14550`，网络链路没有有效波特率；可选的板载
`UART1↔TELEM2/921600` 因 `MAV_1_CONFIG=0` 尚未启用。默认门禁只要求当前两条主链路参数
一致，并把 UART 备链路作为警告；只有显式加入 `--require-uart-fallback` 才把它提升为必需项。
该审计不打开任何硬件，固定报告发送消息和参数写入均为0；物理连通仍需后续台架证明。

在真实 V6X 参数保持不变的前提下，以下入口会在带固定名称、标签、digest 和回环端口边界的
一次性 PX4 固定翼 SITL 容器中测试合法最小超时与 Hold 动作：

```powershell
.\scripts\run_px4_sitl_datalink_loss_acceptance.ps1
```

脚本使用 `14652` 接收仿真遥测，只把 GCS 心跳入口映射到
`127.0.0.1:18570/udp`，并保护地面站端口 `14550`。它必须先证明 GCS 链路健康，再仅在
容器内设置 `COM_DL_LOSS_T=5`、`NAV_DLL_ACT=1`、上传三点 HIL 航线并进入 `MISSION`。
停止有界心跳后必须同时观察到 `gcs_connection_lost=True`、failsafe 和固定翼
`LOITER/Hold`；恢复心跳后必须清除丢链标志，但继续保持 `LOITER`，等待操作者明确改变模式，
不会自动回到 `MISSION`。2026-07-14 的验收通过，Multi-Detect 发送消息0、物理载荷动作0、
真实 V6X 未接触，证据为
`artifacts/evaluation/px4-fixed-wing-sitl-datalink-loss-acceptance-20260713T233800Z-230784.json`。

### 本机 QGroundControl + GR01 只读诊断

当前 Windows 网页代理会被 QGroundControl Daily 错误继承到原始 MAVLink TCP socket，
导致直连 `192.168.144.11:5760` 报“代理类型无效”。自定义 QGC 源码已让 TCP vehicle link
显式使用 `QNetworkProxy::NoProxy`。本机只读启动器会优先选择经过 `cmake --install` 部署的
`QGroundControl-MultiDetect/build-multidetect-release/staging/bin/MultiDetectGCS.exe`，找不到时
才回退到 Daily QGC；不能把只有 EXE、缺少 Qt DLL/QML 插件的原始 `Release` 目录当作发布物：

```powershell
.\scripts\start_qgc_gr01_readonly.ps1
```

脚本先确认 GR01 TCP 可达，再把飞控遥测转到 QGC 默认本机 UDP 端口。反向只允许心跳、
时间同步、参数读取、任务读取和只读能力查询；参数写入、飞行命令、任务写入、执行器和
载荷消息全部丢弃；只读 MAVLink FTP 还必须严格指向 `system/component 1/1`。关闭 QGC 后
桥接进程自动停止。只检查环境、发布物和 GR01 可达性、不启动程序：

```powershell
.\scripts\start_qgc_gr01_readonly.ps1 -ValidateOnly
```

2026-07-13 真机诊断、原始 QGC 日志、参数备份和未决硬件输入见
`artifacts/evaluation/qgc-gr01-v6x-diagnosis-20260713.md`。该只读入口用于查错和对数，不能
用于遥控校准、参数写入或飞行操作。

2026-07-14 已完成自定义 Windows 发布链修复。原始 `Release` 目录只有 EXE，离开开发环境
会因缺少 Qt DLL 失败；正式 `cmake --install` 现生成完整 `staging/bin` 运行目录和 NSIS
安装器。部署版递归检查 54 个 PE 文件、657 条依赖，缺失项为0；隔离、无界面的
`--simple-boot-test` 返回0且没有新崩溃转储。精简安装器不再携带 `include/lib` 开发文件或
Debug CRT，7-Zip 完整性验证通过，SHA-256 为
`12139494bc39160af6041b17f5a778c5fb9164922d7edbfdce32fae67ec3960f`。该本地安装器尚未做
Authenticode 签名，不能作为生产分发物；主项目548项测试、QGC工具链520项测试和原生协议
自测同时通过。证据见
`artifacts/evaluation/custom-qgc-windows-release-20260714T032337Z.json`。

当前 UART 后备链路 `/dev/ttyTHS1:921600` 已确认节点、权限和驱动正常，但 V6X 尚未从
TELEM2 输出任何字节。以后启用冗余串口时，在 QGC 检查 PX4 的 TELEM2/MAVLink 实例，
不要改查 THS2 或要求外接 TELEM 线。Holybro 早期 MAVLink Bridge 页面曾写 THS0，
但当前 Orin Nano 设备树只启用 THS1/THS2，且新版载板指南明确映射到 THS1。

需要生成 `airframe_bench` 可用的 Pixhawk 证据时，使用 `pixhawk-v6x-bench` 和当前 QGC
静止台架快照。该命令至少采集100份新鲜样本，逐字段比较 QGC，并通过不再接收数据的
缓存快照验证 stale 后失败闭锁；完整格式和命令见
[`integration-input-checklist.md`](integration-input-checklist.md#c-第三批pixhawk-v6x只读接入)。

### 官方 PX4 固定翼 SITL 只读验收

Windows + Docker Desktop 可以用一条命令运行官方预编译 PX4 固定翼 SIH 进程、严格
`pixhawk-check`、确定性内存帧 Live 门禁测试和断链反向检查：

```powershell
.\scripts\run_px4_sitl_readonly_acceptance.ps1
```

脚本固定镜像 digest，设置 `PX4_SIM_MODEL=sihsim_airplane`，只向主机独立 UDP `14650`
发送仿真遥测；`14550` 被明确保留给 Mission Planner/QGroundControl，脚本会比较该端口
运行前后的所有者。视觉输入固定为 `synthetic://patrol`，不会枚举或打开电脑摄像头，也不
连接 RTSP；它不替换或删除已有同名容器，只停止自己成功创建的容器。

2026-07-13 的重复验收识别到 PX4 `MAV_TYPE_FIXED_WING`、`MAV_STATE_STANDBY`，严格链路和
位置资格通过，所有接收路径 `messages_transmitted=0`。随后恒定输出合成模型在90帧中
产生85帧 `flame` 候选；由于飞机未解锁且处于 `LOITER`，任务始终为 `standby`，授权、告警
投递、载荷事件和模拟载荷循环全部为0。停止容器后，同一个新鲜链路/位置门禁返回失败，
证明断链不会沿用旧的“可用”状态。结构化证据写入
`artifacts/evaluation/px4-fixed-wing-sitl-readonly-acceptance-*.json`，逐帧候选和审计使用同一
run ID。

当前固定镜像对应 PX4 `v1.18.0-beta1`，且 PX4 官方将固定翼 SIH 标为实验性能力；因此这只
是软件兼容性和失败闭锁证据。取得真实 V6X 固件版本后必须使用匹配构建复测，它不能证明
真实飞机气动、发射、传感器标定、风场、地形、载荷弹道或现场安全。

完整 AUTO 航线与数传失联动作分别运行：

```powershell
.\scripts\run_px4_sitl_auto_mission_acceptance.ps1
.\scripts\run_px4_sitl_datalink_loss_acceptance.ps1
```

前者的任务上传、解锁和模式切换只存在于脚本刚创建的一次性容器；Multi-Detect 仍是零发送
观察器。其视觉侧同样固定使用 `synthetic://patrol`，并在900帧任务中同时验证统一目标池、
短时光流跟踪、巡检建议和轻量单目避障，不访问任何摄像头。AUTO飞行过程中还会运行
`patrol-reacquisition-sitl`：保持10条轨迹和后台锁定，验证短遮挡恢复、保守LOST、强ReID
同身份重捕获及返回观察建议。通用PX4遥测不能证明围栏健康，因此建议保持`DEGRADED`且
要求人工确认；应用仍发送0条MAVLink。后者额外以回环 GCS 心跳证明
`MISSION → LOITER/Hold → reconnect while retaining LOITER`，并在成功或失败时解锁、停止、
等待 `--rm` 自动删除及检查测试端口释放。两者都不是实机固件匹配、气动或现场安全证据。

正向巡检门禁必须显式选择：

```powershell
.\scripts\run_px4_sitl_readonly_acceptance.ps1 -IncludeInContainerArmedPatrolHil
```

该选项只对脚本本次新建且带专用标签的 Docker 容器执行
`px4-commander arm -f → mode auto:loiter → disarm -f`。随后观察器按
`standby → navigating → searching` 前进，并在连续火情候选后只发出一次巡检告警；授权、
载荷动作和 Multi-Detect MAVLink 发送仍为0。没有航线时 PX4 会拒绝 `auto:mission`，所以
此选项明确记录 `auto_mission_validated=false`，不能当作完整 AUTO 航线验证。

单独调试巡检重捕获命令时，只允许使用脚本拥有的隔离本地UDP端口，并必须显式确认：

```powershell
multi-detect patrol-reacquisition-sitl `
  --endpoint udpin:0.0.0.0:14652 `
  --acknowledge-owned-disposable-sitl `
  --out artifacts/evaluation/patrol-reacquisition-sitl.json
```

该命令拒绝地面站端口14550、串口和非本地UDP端点；不打开摄像头、不运行模型、不发送
MAVLink，并要求PX4固定翼身份、新鲜定位、已解锁`MISSION`和至少5 m/s地速。

在没有 V6X 时，也可以先用两个终端验证真实 pymavlink 编码、UDP、字段映射和新鲜度逻辑。
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
证明通用遥测不会被误当成部署许可。此检查不能替代官方 PX4 SITL 或真实 V6X/QGC
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
  --pixhawk-endpoint udp:0.0.0.0:14550 --pixhawk-baud 921600 \
  --pixhawk-system-id 1 --pixhawk-expected-autopilot px4 \
  --pixhawk-expected-vehicle-type fixed_wing --require-pixhawk-operational-state \
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

### ReID 实时准入门禁

人员或车辆 ReID 在实时命令中默认必须同时提供固定哈希 ONNX 和当前 Orin 构建、带溯源清单的
TensorRT 引擎。只提供 `--person-reid-onnx` 或 `--vehicle-reid-onnx` 会在打开摄像头前失败，
防止 CPU provider 在同步逐帧路径造成持续积帧。离线实验确有需要时可显式添加
`--allow-nonrealtime-reid`；运行状态会把该域标为 `onnx_nonrealtime`，并输出
`reid_realtime_admission_passed=false`、`reid_frame_backlog_risk_accepted=true`。该豁免不能用于
Jetson 正式启动器、部署验收或实时能力声明。

实时调度使用两层上限，避免 ReID 串行调用形成无界帧积压：

- `--person-reid-frame-stride 2` 与 `--vehicle-reid-frame-stride 2` 只在对应身份域轨迹稳定时生效；
- 稳定期人员域使用偶数相位、车辆域使用奇数相位，避免两套模型集中在同一帧；
- `--reid-maximum-interval-seconds 0.1` 保证低帧率或采集抖动时不会仅依靠帧号长期跳过；
- 对应域出现 `OCCLUDED`、`REACQUIRING` 或 `LOST` 轨迹时立即强制执行 ReID；
- Kalman、光流/模板提示和统一目标池仍逐帧更新；没有严格外观证据时 `LOST` 身份仍不得恢复；
- 完成事件必须检查每域 `*_reid_inferences`、`*_reid_skipped_frames`、
  `*_reid_no_candidate_frames`、`*_reid_forced_recoveries` 和
  `*_reid_latency_p50_ms/p95_ms`，并结合捕获队列高水位判断积压；
- 没有该身份域候选框的帧不会调用编码器，也不会刷新最长间隔计时，因此候选重新出现时不会
  被稳定期 stride 延后。

Jetson 启动器可通过 `PERSON_REID_FRAME_STRIDE`、`VEHICLE_REID_FRAME_STRIDE` 和
`REID_MAXIMUM_INTERVAL_SECONDS` 调整，但真实 RTSP 验收前不得放宽到使重捕获超过 0.5/2 秒门禁。

维护窗口不允许“只构建、不推理”。确认飞机处于地面、识别进程已由操作者有意停止后，使用：

```bash
export MULTIDETECT_REID_MAINTENANCE_ACK=recognition-stopped-ground-maintenance-only
/home/jetson/Multi-Detect/scripts/run_jetson_perception_engine_maintenance.sh
```

脚本自身不会停止或重启识别进程；只要发现 `multidetect live-camera` 仍运行就拒绝开始，并用
`flock` 防止两个构建任务并发。它依次构建通用检测、人员 ReID 和车辆 ReID 引擎，复核源模型、
引擎哈希和目标机溯源，最后运行 `reid-tensorrt-bench`。该门禁使用混合的 4 人、4 车和非身份
类别输入，检查批处理维度、身份域隔离、L2 特征、重复推理稳定性、人员/车辆 P50/P95、稳定期
错峰 P95 和恢复期串行 P95。恢复期串行 P95 超过默认 66.7 ms 时命令返回非零；生成引擎文件
本身不算通过。报告仍明确 `deployment_domain_accuracy_validated=false`，真实录像 IDF1/IDSW
必须单独验收。

## Jetson 优化验收顺序

1. 在录像回放上验证 ONNX 模型确实是 post-NMS Nx6，逐框比较类别、置信度和坐标。
2. 在 Orin Nano 上记录 CPU/GPU、JetPack、CUDA、TensorRT、ONNX Runtime provider、模型和 engine 哈希。
3. 测量 RTSP 解码到显示的 P50/P95 延迟、断流重连、丢帧率、GPU/内存和热降频。
4. 单独接入人员/消防员/车辆/建筑等安全模型，验证 `person_detector_healthy` 与漏检降级。
5. 将 Pixhawk 遥测先作为只读监控，与 QGroundControl/飞控日志逐字段核对。
6. 先用签名文件 HIL 验证地理围栏/投放区证据的任务、位置、时间和重放绑定，再替换为经过独立验证的真实安全源。
7. 只有完成持久化审计、真实地理围栏/投放区安全源、独立硬件互锁和受控 SIL/HIL 后，才另行设计真实执行器接口；本仓库当前没有此接口。
