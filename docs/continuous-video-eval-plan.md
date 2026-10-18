# 连续视频测距采集计划

本计划使用采集器的 `--video-hz 10` 可选模式。默认不传该参数时，原来的步进录像格式、教师动作和动作节奏不变。视频帧写入每个 episode 的 `video/`：`rgb/*.jpg`、`seg/*.png`、`depth/*.npz` 和逐帧 `manifest.jsonl`；`video/video.json` 记录帧数、字节数、估算每分钟占用和是否触发上限。

## 覆盖范围

选三个世界：`fuel_v1` 稀疏目标世界、障碍物密集世界、远距离目标世界。每个世界 20 架次，共 60 架次；每架次目标连续飞行约 3 分钟，其中动作执行期持续采样，预计得到约 1800 帧/架次、30 小时视频。RGB 采用 640x480 JPEG，深度 320x240 压缩 NPZ，分割仅在启用分割 overlay 的批次采集。

按实测保守预算 1.0--1.5 GiB/分钟，单架次上限设为 4 GiB；60 架次的原始视频上限约 240 GiB。采集根目录预留至少 260 GiB，命令行的 `--min-free-gb 15` 仍然作为全局安全底线。单架次超过上限只停止视频写入并在 manifest 标记 `max_video_bytes`，不会停止教师动作。

## 完整命令

```powershell
$env:TEMP=$env:TMP=$env:TMPDIR='D:\Projects\mdwt\videorec\.tmp'
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
$env:CUDA_VISIBLE_DEVICES='-1'
$env:PYTHONPATH='src'

python scripts/collect_sim_teacher_episodes.py `
  --scenario simulation/scenarios/fuel_v1.json `
  --teacher explore --episodes 20 --steps-per-episode 80 `
  --video-hz 10 --video-max-gb 4 --segmentation `
  --depth-format npz --compress-mavlink `
  --min-free-gb 15 --out artifacts/datasets/continuous-video/fuel_v1

python scripts/evaluate_onboard_ranging_reuse.py `
  artifacts/datasets/continuous-video/fuel_v1 `
  --video-only --max-episodes 20 `
  --output tmp/ranging-eval/fuel_v1-video.json
```

对另外两个世界只替换 `--scenario` 和输出目录，重复上述命令。仿真恢复后按世界串行执行；慢速 PX4/Gazebo 每架次预计 8--12 分钟，60 架次约 8--12 小时，评估在 CPU 上预计每架次数分钟。先运行 1 架次验证磁盘估算，再扩大到完整批次。

## 评估输出

评估器从连续 manifest 读取 RGB、位姿、姿态、速度和仿真时间戳，使用姿态投影得到 `CameraMotionEstimate`，分别运行 RGB-SLAM 和视觉惯性估计器；单目近距深度按 10 Hz 视频抽样。结果同时报告误差、有效率、拒绝原因、CPU 耗时，并按距离（<10、10--20、20--40、>=40 m）和飞行阶段分桶。步间三角化和地面相交仍由旧评估路径报告，便于直接比较。

## 试采结果（2026-10-18）

计划里的 60 架次 / 约 240 GB 放不下，先做了 3 个测试世界（gen_v2_w0034、w0037、w0039）× 2 架次的试采：专家教师、10 Hz、RGB + 深度，输出 `D:/Projects/Multi-Detect/artifacts/datasets/continuous-video-pilot/`（报告 `ranging-video-eval.json`）。

先修了一个让视频模式实际不工作的问题（`001b595`）：原来要求图像时间戳不早于最新位姿才采样，而图像总是比位姿晚一点，结果动作执行期间一帧都没写，只剩每个动作结束时的那一帧（第一次试采 2.5 分钟只得到 7 帧）。修好后每架次 186–1,008 帧，共 2,875 帧；10 Hz 连续采样实测约 37–45 MB/分钟（640×480 JPEG + 压缩深度，不含分割），远低于计划里 1–1.5 GB/分钟的估算，磁盘不再是限制。

平地、地面高度已知时（世界本身是平地）：

| 方法 | 有效输出率 | 平均误差 | 每架次 P95 |
|---|---:|---:|---:|
| 地面相交（机上现用） | 71% | **2.33 m** | 2.8–4.9 m |
| 早期 RGB-SLAM（姿态单应估计相机运动） | 57% | 3.83 m | 7.5–25.5 m |
| 早期视觉惯性 | 57% | 4.00 m | 6.9–28.2 m |
| 单目度量深度（每 10 帧一次） | ≈0 | 42 m | 34–100 m |

按距离：10–20 m 时 RGB-SLAM / 视觉惯性只有 0.3 / 0.4 m（地面相交 1.4 m），但只有 33 个样本；20–40 m 和 40 m 以上都比地面相交差（4.1–4.4 m vs 1.6 m；3.8–3.9 m vs 2.9 m）。单目深度在这些距离上不可用（与 10 m 以内才准的早先结论一致；专家飞行几乎不进入 10 m 内）。评估器里的“step_triangulation”是相邻视频帧直接交会，基线极短，不是机上带视差门的步间三角化，不代表机上结果。

结论：早期 RGB-SLAM / 视觉惯性暂不接入机上；近距离（<20 m）有潜力，等有更多近距样本、并能在“地面高度未知”条件下评估时再决定。机上测距保持“地面相交 + 步间三角化（默认开启）”。局限：只有 6 架次、3 个世界，且全是平地。
