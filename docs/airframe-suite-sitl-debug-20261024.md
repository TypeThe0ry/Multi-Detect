# 多机型回归套件 SITL 离线排查（2026-10-24）

本轮只做离线检查和修复，没有启动 SITL、Docker 容器或 llama-server。`artifacts/` 仅用于读取既有日志。

## 已证实根因

`eval_onboard_loop_campaign.py` 的 `--segmentation` 默认开启，首次采集和失败重试都会把 `--segmentation` 传给 collector。启用后，frame server 使用 `ExactSegmentationPairer`，RGB 帧必须找到同仿真时间戳的 segmentation labels 帧才会成为候选、写入 wire stream。此前 `docker_command()` 却把 segmentation 覆盖层固定挂载到 `x500_mono_cam`；`standard_vtol_mono_cam` 和 `rc_cessna_mono_cam` 的基础组合模型也没有 segmentation sensor。因此这三个 suite 任务在启用分割时都没有满足首帧契约的模型资产。

夜间 rerun 日志直接记录 frame server 收到 300 张 RGB 图像、没有发出任何同步帧，最后报 `no synchronized frame newer than -1` 并退出。collector 随后在 MDGZSIM3 固定头读取处看到 `expected 276 bytes, received 0`。276 是 wire header 固定长度；它不是图像 payload 大小或分辨率。该错误是 frame server 未写出首条记录后的 EOF 表现。

静态核对也确认 suite airframe 到模型映射正确：`standard_vtol` 对应 `standard_vtol_mono_cam_0`，`rc_cessna_runway` 对应 `rc_cessna_mono_cam_0`。两个原始模型都含 RGB `camera_link` 与 co-located depth camera，collector 的 image/depth topic 拼法与 SDF 一致。Cessna runway profile 允许 `runway` 起飞且设置 `RWTO_TKOFF=1`，runtime 的 `spawn_id` 和地面姿态与固定翼跑道文档配方一致。因此机型别名、RGB/depth topic、尺寸和起飞方式不是本次已见失败的静态原因。

## 推测与未证实项

夜间日志对 frame server 的汇总没有 segmentation 回调数或未配对数，无法仅从旧日志证明每张 RGB 都具体因缺少 segmentation 帧被丢弃；该结论由默认参数、pairer 行为和缺少传感器资产共同推出。此次修复补上静态缺口，动态 timestamp 配对是否成功仍须 SITL 验证。

尚未在本机镜像内做只读文件检查，因此模型合并后的 Gazebo scoped topic 是否与静态 SDF 推导完全一致、`labels_map` 实际发布频率/时间戳是否可配对，仍需运行时日志确认。若修复后仍没有 wire 首帧，下一步应核对 `sim_teacher_frame_server_finished` 汇总和订阅 topic，而不是把 276 字节误判为 RGB/depth 尺寸错误。

## 修复内容

- 新增 `standard_vtol_mono_cam_seg`、`rc_cessna_mono_cam_seg` 模型覆盖层，保留原 RGB/depth 相机安装并添加同位的 1280x960 instance segmentation sensor。
- collector 根据 runtime 映射出的 Gazebo 模型选择对应 segmentation overlay；x500 的 wind+segmentation 组合继续使用既有专用 overlay。
- 新增离线测试，逐项验证 suite 中模型的 profile 解析、RGB/depth 资产、segmentation overlay 与 topic。

## 待执行 SITL 验证

以下命令只运行 suite-airframes 的三个任务各 1 架次，磁盘下限为 8 GiB。脚本经共享锁取得 SITL 所有权；锁被 r20 持有时会等待。请在队列轮到本任务后执行：

```bash
cd /d/Projects/mdwt/cx-vtol-stream
source /d/Projects/mdwt/_scratch/sitl_lock.sh
sitl_acquire airframe-suite-20261024
trap sitl_release EXIT
export PYTHONPATH='src;.'
export PYTHONUTF8=1
python scripts/run_mission_suite.py \
  --suite configs/onboard/suite-airframes.json \
  --out /d/Projects/Multi-Detect/artifacts/benchmarks/airframe-suite-20261024-fix \
  --only standard-vtol-w0016-ambulance-return rc-cessna-w0016-ambulance-known-limit rc-cessna-w0031-ambulance-negative \
  --episodes 1 \
  --min-free-disk-gb 8 \
  --strict
```

验收时确认三项均产生 episode；frame server 输出首帧，且其 `image_topic`、`depth_topic`、segmentation 配对无拒绝；Cessna 两项还应看到 runway 起飞并采到图像。保留完整 campaign log 和 frame server 的 `sim_teacher_frame_server_finished` 记录作为动态证据。
