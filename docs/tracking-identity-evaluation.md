# 身份跟踪与遮挡恢复评估

## 1. 用途与证据边界

`evaluate-tracking` 对已经完成逐帧身份标注的录像结果计算 IDF1、ID Precision、ID Recall、
ID Switch、碎片化、MOTA、匹配 IoU，以及遮挡和出画后的同 ID 恢复率与恢复延迟。

评估器只读取 JSONL 和可选源视频并计算哈希，不打开摄像头、模型、Pixhawk、飞控或执行器。
它不能从普通检测框推断真实身份，也不能把没有人工身份标注的图像数据集当成跟踪准确率证据。

仓库中的 `tracking_identity_*.demo.jsonl` 是 6 帧合成计算器样本。其报告即使为满分，也必须
保持：

- `dataset_provenance=synthetic_demo`
- `annotations_reviewed=false`
- `deployment_domain_evidence_complete=false`

当前仓库尚无经过复核的部署域身份录像标注，因此真实部署域 IDF1 和 ID Switch 仍是未完成项。

## 2. 身份真值格式

每一行表示一帧，帧时间戳必须严格递增：

```json
{
  "frame_id": "camera-main-000001",
  "captured_at_s": 10.033,
  "objects": [
    {
      "identity_id": "vehicle-017",
      "label": "vehicle",
      "visibility": "visible",
      "bbox": [0.12, 0.25, 0.31, 0.62]
    }
  ]
}
```

`visibility` 只能是：

- `visible`：必须提供归一化 `xyxy` 框；
- `occluded`：`bbox` 必须为 `null`；
- `out_of_frame`：`bbox` 必须为 `null`。

同一身份从首次出现到最后一次出现之间的每一帧都必须显式标注，不能通过缺行猜测遮挡还是
出画。同一 `identity_id` 的类别不得变化，每帧身份 ID 不得重复。

## 3. 预测格式

```json
{
  "frame_id": "camera-main-000001",
  "captured_at_s": 10.033,
  "tracks": [
    {
      "track_id": "target-000042",
      "label": "vehicle",
      "bbox": [0.12, 0.25, 0.31, 0.62],
      "state": "tracking",
      "confidence": 0.91
    }
  ]
}
```

只有 `detected / locked / tracking / recovered` 状态进入可见框匹配；`occluded / reacquiring /
lost` 保留在日志中但不作为可见预测。预测和真值必须具有完全相同的 `frame_id`，默认时间差
不得超过 50 ms。

## 4. 指标计算

1. 每帧先按类别和 IoU 门限做矩形 Hungarian 全局匹配。
2. 汇总所有真值身份与预测轨迹的匹配次数，再做一次全局身份分配得到 IDTP、IDFP、IDFN。
3. `IDF1 = 2*IDTP / (2*IDTP + IDFP + IDFN)`。
4. 同一真值身份从一个预测 ID 变为另一个预测 ID 时计一次 ID Switch，即使中间存在遮挡。
5. 可见但未匹配后再次匹配计一次碎片化。
6. 遮挡或出画前必须已有可靠匹配，该事件才进入恢复率分母。
7. 重新可见后恢复到遮挡前同一 `track_id` 才算成功；不同 ID 立即判为失败。
8. 默认短遮挡恢复预算为 0.5 秒，出画重捕获预算为 2.0 秒。

所有指标同时输出总体结果和逐类别结果。若没有可评估样本，相应比例为 `null`，不能按 100%
处理。

## 5. 命令

实时程序启用统一目标池时，可以直接写出与评测器兼容的逐帧身份轨迹元数据：

```powershell
$sessionId = [guid]::NewGuid().ToString()
python -m multidetect live-camera configs/missions/fire_patrol.demo.json `
  --onnx-model models/fire.onnx `
  --unified-target-pool `
  --identity-tracking-log-out artifacts/tracking/identity-tracks.jsonl `
  --identity-tracking-session-id $sessionId
```

该日志不包含像素，也不提供飞控或载荷控制能力。目标池处理失败的帧会记录为空轨迹帧，避免
伪造身份连续性。Jetson 启动器会在下一次受控启动时生成
`jetson-live-<timestamp>.identity-tracks.jsonl`，但必须显式提供
`TRACKING_EVIDENCE_SESSION_ID`，防止识别日志和录像各自生成不同会话号。

将身份轨迹日志与同一次采集的源视频绑定为人工复核草稿：

Jetson 可用独立命令录制摄像头原始 H.265 码流，不解码、不重新编码，也不进入实时识别或
QGC 视频链路：

```bash
export CAMERA_SOURCE='rtsp://camera-address/stream'
export TRACKING_EVIDENCE_SESSION_ID='由受控采集流程生成并与实时识别相同的 UUID'
python -m multidetect record-rtsp-evidence \
  --source-env CAMERA_SOURCE \
  --session-id "${TRACKING_EVIDENCE_SESSION_ID}" \
  --out-video artifacts/tracking/deployment-flight.mkv \
  --manifest-out artifacts/tracking/deployment-flight.manifest.json \
  --duration-seconds 300
```

该工具通过 Python GStreamer API 读取环境变量，RTSP URI 不进入命令行或清单。录制结束必须
收到 EOS、写出非空 Matroska，并经过全帧媒体强校验；失败返回非零状态。它是独立且默认关闭
的证据工具，录像异常不会影响实时感知，且不包含任何飞控或载荷控制路径。

随后生成复核草稿：

```powershell
python -m multidetect prepare-tracking-review `
  artifacts/tracking/identity-tracks.jsonl `
  artifacts/tracking/deployment-flight.mkv `
  artifacts/tracking/deployment-flight.manifest.json `
  artifacts/tracking/review-bundle
```

该命令要求身份日志和录像清单具有相同 UUID，录像 SHA256/字节数与清单一致，并检查身份日志
的单调时间戳位于录像采集窗口内。旧版无会话绑定的清单会被拒绝，不能升级声明为部署证据。
输出包含源视频、录像清单、预测日志和草稿的 SHA256。草稿使用 `review_status=pending`、空
`identity_id` 和空 `visibility`，故意不兼容正式评测输入；必须逐帧对齐视频、独立填写身份、
补齐遮挡/出画时间线并由第二人复核。命令会离线解码全部视频帧，检查 FPS、稳定分辨率、声明
帧数完整性，并要求可解码帧数和时长覆盖身份日志；通过后输出
`source_video_media_decoding_validated=true` 和
`source_video_track_timeline_coverage_validated=true`。这仍不代表逐帧对应关系已人工确认，清单固定
保留 `video_frame_alignment_reviewed=false`。默认拒绝覆盖已有草稿，只有显式 `--overwrite`
才会替换。

计算器演示：

```powershell
python -m multidetect evaluate-tracking `
  examples/tracking_identity_ground_truth.demo.jsonl `
  examples/tracking_identity_predictions.demo.jsonl `
  --dataset-provenance synthetic_demo `
  --minimum-idf1 0.9 `
  --maximum-id-switch-count 0 `
  --minimum-occlusion-recovery-rate 0.9 `
  --minimum-out-of-frame-recovery-rate 0.9 `
  --maximum-occlusion-recovery-p95-seconds 0.5 `
  --maximum-out-of-frame-recovery-p95-seconds 2.0 `
  --out artifacts/evaluation/tracking-identity-calculator-demo-20260715.json
```

部署域录像必须同时提供源视频、身份真值、预测结果和人工复核声明：

```powershell
python -m multidetect evaluate-tracking `
  artifacts/tracking/deployment-ground-truth.jsonl `
  artifacts/tracking/deployment-predictions.jsonl `
  --dataset-provenance deployment_recording `
  --source-video artifacts/tracking/deployment-flight.mp4 `
  --annotations-reviewed `
  --minimum-idf1 0.9 `
  --maximum-id-switch-count 5 `
  --minimum-occlusion-recovery-rate 0.8 `
  --maximum-occlusion-recovery-p95-seconds 0.5 `
  --out artifacts/evaluation/deployment-identity-tracking.json
```

门限未通过时命令返回退出码 2，并在 `failure_reasons` 中列出原因。报告固定包含源文件 SHA256、
数据来源声明、安全边界和 `deployment_domain_evidence_complete`，避免报告与录像或标注脱离。

## 6. 部署数据采集要求

至少应覆盖：

- 人员与车辆交叉、相似衣着和相似车型；
- 部分遮挡、完全遮挡、出画后重新进入；
- 快速航向变化、运动模糊、逆光、低照度、烟雾和低纹理；
- 至少 10 个并发目标、主目标切换和后台轨迹保持；
- 原始帧时间戳、模型版本、ReID 制品哈希和相机参数。

身份标注必须由第二人复核；对无法确认的身份应标为不纳入评估，而不能猜测 ID。完成这些输入
之前，合成报告只证明评估器正确，不证明系统已经达到部署精度。

## 7. Jetson 验证记录

2026-07-15 已将共享 Hungarian 分配器、身份评测器、统一目标池接入和 CLI 原子同步到
Jetson。远端 `py_compile` 与合成验收门槛通过，在线识别进程同步前后均为 PID 7767，未重启，
因此新模块尚未进入该在线进程。机器可读记录位于
`artifacts/evaluation/jetson-tracking-evaluator-verification-20260715T001111Z.json`；该记录明确
保留 `annotations_reviewed=false` 和 `deployment_domain_evidence_complete=false`。

随后已补齐实时身份轨迹日志和人工复核数据包，远端编译、启动器语法与 CLI 检查通过，PID
7767 仍未重启。机器可读同步记录位于
`artifacts/evaluation/jetson-identity-evidence-pipeline-sync-20260715T003612Z.json`。只读审计确认
旧在线配置没有生成 `*.identity-tracks.jsonl`，因此目前没有可用于真实 IDF1 的旧日志。

视频强校验已分别在 Windows 和 Jetson 上使用真实 OpenCV MJPG 文件完成全帧扫描，均解码
12 帧并确认 10 FPS、320×240 和身份时间轴覆盖；精确帧对齐仍保持未审核。机器可读证据位于
`artifacts/evaluation/jetson-video-evidence-validation-20260715T004829Z.json`。该合成媒体只验证
探测器实现，不是部署域录像或跟踪准确率证明。

随后对真实 RTSP 摄像头完成一次 5 秒 H.265 码流直拷贝冒烟测试：Matroska 文件 68,191
字节，SHA256 校验一致，GStreamer 识别为 H.265 Main Profile，OpenCV 全帧解码 103/103 帧，
25 FPS、1280×720。清单未包含 RTSP URI；并发在线 PID 7767 的 RSS 前后均为 754,048 KB，
最近 500 条审计中相机读取、推理和目标池失败均为 0。证据位于
`artifacts/evaluation/jetson-real-rtsp-stream-copy-smoke-20260715T010559Z.json`。由于旧在线进程
仍未输出身份轨迹日志，且该冒烟录像早于会话绑定清单 v2，这段短视频不能用于 IDF1，也不替代
受控重启后的同会话部署域采集。

证据会话强绑定随后已同步到 Jetson：身份日志逐帧携带 UUID，录像清单升级为 schema v2 并记录
单调时间窗，复核包会拒绝跨会话、视频哈希/大小不一致、时间窗越界及旧版无绑定清单。本地
813 项测试、Ruff 检查和格式门通过；远端编译、脚本语法和两个 CLI 入口检查通过，在线 PID
仍为 7767。记录位于
`artifacts/evaluation/jetson-evidence-session-binding-sync-20260715T013127Z.json`。真正同会话的
视频与身份轨迹仍需在受控维护窗口重启后采集。

短时恢复路径同时加入最后可靠观测模板缓存和状态自适应搜索：遮挡与重捕获阶段分别扩大局部
相关搜索，模板超过时效立即失效；LOST 轨迹不运行该回退，必须等待严格 ReID/检测证据。模板
相关结果只作为 Kalman 预测提示，重新出现的检测仍需通过目标池门禁才能恢复原身份。

不连接硬件的目标池性能基准可单独运行：

```powershell
python -m multidetect unified-tracking-bench `
  --track-count 10 `
  --benchmark-frames 3000 `
  --minimum-metadata-rate-hz 15 `
  --maximum-switch-latency-ms 200 `
  --out artifacts/evaluation/unified-tracking-core-bench.json
```

该命令包含 10 目标持续关联、后台锁定、周期主目标切换、短遮挡、LOST 无 ReID 拒绝、强 ReID
恢复、交叉目标、近似身份歧义、Kalman 预测、Hungarian 分配和置信度级联；报告的整段墙钟吞吐
也必须达到门限。它不运行图像模型，因此不能替代真实录像身份评测。

OpenCV 图像级短时恢复可单独运行：

```powershell
python -m multidetect short-term-tracking-bench `
  --track-count 10 `
  --benchmark-frames 300 `
  --analysis-width 320 `
  --frame-stride 2 `
  --maximum-processing-latency-p95-ms 66.7 `
  --maximum-recovery-seconds 0.5 `
  --out artifacts/evaluation/short-term-image-tracking-bench.json
```

该基准使用真实光流/模板算法和合成纹理帧，验证缓存上界、完全遮挡、扩大搜索、大位移重现、
运动提示及原 ID 恢复；不会打开摄像头、模型或 Pixhawk。

同一相机运动前端的单目风险图像级验收可单独运行：

```powershell
python -m multidetect monocular-avoidance-bench `
  --benchmark-frames 300 `
  --analysis-width 320 `
  --maximum-processing-latency-p95-ms 66.7 `
  --minimum-end-to-end-rate-hz 15 `
  --out artifacts/evaluation/monocular-avoidance-image-bench.json
```

该命令运行真实 OpenCV LK 光流和 RANSAC，验证静态场景、全局相机平移、中心障碍径向逼近、
分区 TTC、陈旧证据闭锁和性能门限。它不打开摄像头、模型或 Pixhawk，且报告固定声明
`metric_depth_available=false`、`flight_control_enabled=false`；合成图像通过不能替代真实障碍
数据集或飞行验收。

固定哈希的人员/车辆 ReID ONNX 制品可执行 CPU 兼容性基准：

```powershell
python -m multidetect reid-onnx-cpu-bench `
  --person-model models/reid/nvidia-tao-reidentificationnet-v1.2/resnet50_market1501_aicity156.onnx `
  --vehicle-model models/reid/openvino-vehicle-reid-0001/osnet_ain_x1_0_vehicle_reid.onnx `
  --person-count 4 `
  --vehicle-count 4 `
  --iterations 2 `
  --out artifacts/evaluation/reid-onnx-cpu-bench.json
```

该命令只允许 CPU provider，不会隐式构建 TensorRT 引擎。它检查制品摘要、动态批量、混合类别
隔离、嵌入维度、L2 归一化、重复稳定性和墙钟延迟；`passed=true` 只表示模型契约正确，实时性
必须单独查看 `realtime_budget_passed`。CPU 未通过实时预算是预期的否定证据，不能据此启用在线
ReID；最终仍需维护窗口内在实际 Orin 上构建并验证 FP16 TensorRT 引擎。
