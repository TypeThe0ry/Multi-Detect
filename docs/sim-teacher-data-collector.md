# SIM 教师数据采集器

本工具在固定 digest 的 Gazebo + PX4 SITL 容器中采集真实的 `observation -> action -> next observation` 样本。当前只支持 `x500_mono_cam` 多旋翼。运行时只记录原始流；文件哈希和完整性检查由离线 auditor 完成。

## 2026-10 对齐修复

旧版 `scripts/gazebo_sensor_stream.py` 把完整 RGB（1280×960）和 FLOAT32 深度帧写入有界 FIFO。`docker exec` stdout 消费速度低于 Gazebo 产帧速度时，生产者会阻塞，collector 随后按 FIFO 读到起飞初期的旧图像。PX4 `LOCAL_POSITION_NED.time_boot_ms` 已经前进而图像仍停在起飞垫，导致图像与位姿错配。

该 frozen helper 被 `src/multidetect/release_parity.py` 和 `docs/current-progress.json` 绑定，因此没有修改它。采集器现在启动 collector-owned `scripts/sim_teacher_frame_server.py`：Gazebo 回调只保留一个最新的完整 RGB/depth 同步候选，替换旧候选时计数并丢弃；collector 在每个 `obs_before`/`obs_after` 请求 `GET <pose_sim_stamp_ns>`，服务端只返回模拟时间戳不早于该请求且尚未发送过的帧。默认 RGB 为 640×480、depth 为 320×240 `UINT16_MM`；可用 `--rgb-size`、`--depth-size` 和 `--depth-encoding` 切换，输出 intrinsics 会按缩放比例记录。完整分辨率仍可显式选择。

每个 live observation 记录 `frame_sensor_stamp_ns`、`pose_time_boot_ms`、`pose_sim_stamp_ns` 和 `sim_skew_ms`。auditor 默认拒绝 `abs(sim_skew_ms) > 250`，并拒绝位姿移动超过 1 m 而 frame stamp 没有严格前进的 episode。这个门会直接捕获 r5 的 backlog 模式。

历史 `urban-explore-r12`、`urban-expert-r1` 到 `urban-expert-r5` 的图像已经与位姿错配，禁止用于训练；必须重新采集并通过新的 alignment gate。

## 运行

单个 5 步 explore episode：

```powershell
C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe `
  scripts/collect_sim_teacher_episodes.py `
  --scenario configs/scenarios/urban_inspection_multirotor_001.scenario.json `
  --teacher explore --episodes 1 --steps-per-episode 5 --seed 7 `
  --out artifacts/datasets/sim-teacher/urban-explore-r30 `
  --max-episode-s 300 --keep-container-on-failure
```

`--dry-run` 只打印计划，不启动 Docker。`--max-episode-s` 是单 episode 的硬墙钟上限；`--keep-container-on-failure` 仅用于调试，失败时保留容器。默认输出 RGB 为 `640x480`、depth 为 `320x240 UINT16_MM`；需要原始带宽时显式传 `--rgb-size 1280x960 --depth-size 1280x960 --depth-encoding FLOAT32_M`。

## 实际控制流程

1. 启动 digest 固定的 PX4/Gazebo 容器并等待 world。
2. 建立唯一的 `udpin:127.0.0.1:14541` pymavlink 连接，source identity 为 system/component `255/190`。同一个 socket 接收 PX4 telemetry，并发送 GCS heartbeat、arm、模式切换和 offboard setpoint；不建立独立 `udpout` 控制连接。
3. 收到 heartbeat 后，通过 `docker exec` 设置 SIM-only 参数 `COM_RC_IN_MODE=4`，并写入 `episode.json.sim_only_parameters`。不使用 force-arm，也不关闭 PX4 failsafe。
4. 以 0.5 秒周期持续发送 GCS heartbeat。等待 EKF/home/GPS 和 pre-arm health；优先使用 `HEALTH_REPORT`，没有该消息时回退到 `SYS_STATUS`、最近没有 `Preflight Fail` 的 `STATUSTEXT`，并要求至少约 10 秒 heartbeat warm-up。stderr 中每 5 秒输出带单调时间戳的健康位名称。
5. 使用 `MAV_CMD_COMPONENT_ARM_DISARM` (400)，`param1=1`、`param2=0`，在拒绝时有限重试。随后在 OFFBOARD 中起飞、执行策略 primitive，最后 RTL、LAND，并等待 disarmed heartbeat。
6. RGB/depth 使用已有 MDGZSIM3/MDGZDEP1 解码器。没有 live frame 时 episode 失败，绝不写 placeholder observation。

## 数据布局

```text
<out>/
  episodes/<episode_id>/
    episode.json
    steps.jsonl
    mavlink.jsonl
    rgb/*.png
    depth/*.npy
  audit.json
```

每个 step 包含真实的 RGB、float32 米制 depth、RGB camera info、输出 depth geometry/encoding/intrinsics（`depth_camera_info`）、NED pose、经纬高、姿态、速度、GPS fix、动作、ACK、执行结果、frame/pose 时间年龄和下一帧 observation。`mavlink.jsonl` 保存每条收到的 MAVLink 报文和接收时间。

离线审计：

```powershell
C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe `
  scripts/audit_sim_teacher_dataset.py `
  --dataset artifacts/datasets/sim-teacher/urban-explore-r30
```

### 可选紧凑格式

默认输出保持历史格式：`depth/*.npy`（float32、米）、`mavlink.jsonl`，以及
原有 PNG 文件名和内容。以下选项必须显式启用：

- `--depth-format npz` 使用 compressed NPZ，数组键为 `depth`，数值语义仍是米。
- `--depth-format png16` 使用无损 PNG 容器保存 UINT16 毫米值。这个格式是有损量化：
  深度按 1 mm 四舍五入，超过 65.535 m 的值饱和到 65.535 m；读取时必须按毫米解释。
- `--deduplicate-depth` 在连续深度数组完全相同的时候复用上一文件路径。
- `--compress-mavlink` 写入 `mavlink.jsonl.gz`。
- onboard policy 可用 `onboard_frame_format=jpeg` 和 `onboard_frame_quality` 将重复的
  onboard frame copy 写成 JPEG；默认仍为 PNG。

`load_depth_array()` 和 replay/calibration 工具同时支持旧格式与这些 opt-in 格式。
PNG16 的量化损失只影响该格式；NPZ 和 gzip 不改变数组或 JSON 记录语义。

alignment gate 的默认上限是 250 ms、位姿移动门槛是 1 m；需要审计回放或调整门槛时可传 `--max-sim-skew-ms` 和 `--pose-motion-threshold-m`。

审计生成 SHA-256 manifest，检查文件、step 配对、有限 pose、GPS、ACK、时间单调性、frame/pose skew、成功率、动作直方图、覆盖范围、episode 时长和 achieved-vs-commanded 误差。发现损坏时返回非零状态。

## teacher 策略和范围

`explore` 使用 seed 控制的随机平移、转向、hold 和 stop，并遵守场景边界与高度限制。`expert_search` 使用 scenario truth 做 lawnmower/expanding search、FOV 几何判断和 standoff approach，并记录 `target_in_fov`、`target_bearing`、`target_range` 等 privileged labels。

## 适用范围

这些样本只证明 Gazebo PX4 SITL 采集链路和 SIM 控制执行。它们不声明 Jetson/Pixhawk 实机、户外 GNSS、感知精度或 sim-to-real 性能。scenario truth 只供 privileged expert policy 使用，不会伪装成机载视觉观测。

## expert_search 教师行为（当前实现）

`expert_search` 使用固定的、与隐藏目标坐标无关的搜索先验。默认搜索半径为 120 m，航段长度不超过 15 m；策略沿有界 lawnmower 航线飞行，并在每个航点原地以 90° 增量扫描一整圈。搜索区域来自任务场景配置，不表示教师知道目标位置，也不会在目标尚未进入相机几何视场前直接朝目标飞行。

相机为前视相机，FOV 判断使用采集器的 1280×960 标定内参（`fx=fy=539.936368...`、主点 `(640,480)`），并限制探测距离为 80 m。目标投影进入图像后进入 `approach`：先把机头转到目标方位（允许 ±10°），再以不超过 15 m 的步长接近，直到默认 15 m standoff；随后 `hold` 2 s，最后 `done/stop`。每步标签包含 `phase`、`target_in_fov`、`target_bearing_deg`、`target_range_m`。

位姿执行器保存绝对高度和偏航的上一条**指令参考**，下一条 setpoint 从该参考叠加显式命令，因此测量噪声不会累积成高度或偏航漂移；记录中的 achieved 值仍来自实测遥测。当前几何 FOV 尚未建模遮挡和真实 line-of-sight，这是已知限制；这些样本只声明 Gazebo/PX4 SIM 控制链和 privileged teacher 标签，不声明真实相机、GNSS 或 sim-to-real 性能。


## Opt-in segmentation supervision

Generate Fuel worlds with `--segmentation-labels`, then collect with
`--segmentation`. The collector mounts
`simulation/gazebo/model-overlays/x500_mono_cam_seg` read-only over
`/opt/multidetect-gazebo-models/x500_mono_cam` for this container only.
The original model and PX4 model/autostart selection remain evidence-bound.
The additional `segmentation` sensor uses `<camera><segmentation_type>instance`
and shares the RGB optical frame, 1280x960 image geometry, 1.74 rad horizontal
FOV, 0.1..3000 m clipping range, and 30 Hz update rate.

The frame server subscribes to `/multidetect/teacher/segmentation/labels_map`.
It pairs RGB and segmentation at exactly the same simulation timestamp;
missing or stale segmentation cannot produce a recorded observation.
Both channels use the same nearest-neighbour resize when `--rgb-size` is
smaller than the source. Use `--rgb-size 1280x960` for small-object supervision.
The labels map is stored as an RGB uint8 PNG in `seg/000003_after.png`, with
`segmentation_path` and `segmentation_info` in `steps.jsonl`. PNG channel bytes
are unchanged: R + 256*G is the instance id, B is the semantic label. This is
a label encoding, so do not treat it as an ordinary color segmentation preview.
The truth `segmentation_labels` map assigns each object its own semantic id.

Build detector labels with `scripts/build_sim_detector_dataset.py
--label-source segmentation`. The builder requires matching segmentation and
truth mapping, derives half-open pixel boxes from visible components, and
applies `--min-seg-area-px` and `--min-box-px`. Projection visibility thresholds
do not apply to this path. The SFT builder automatically uses segmentation
candidate boxes when a recorded segmentation path exists. Default collection
and detector labels continue to use the existing RGB/depth and projection path.
Segmentation is offline supervision; it is not a policy perception input.

For a visual check, run:

```powershell
python scripts/overlay_sim_segmentation_boxes.py --rgb <episode>/rgb/000003_after.png `
  --seg <episode>/seg/000003_after.png --truth <world>.truth.json --out artifacts/seg-overlay.png
```

The encoding follows the Harmonic instance-segmentation tutorial and the
repository annotation reader. The pinned container's actual sensor/topic,
RGB mount intrinsics, nested model labels, and synchronized renderer output
still require a live smoke test when Docker is available.
