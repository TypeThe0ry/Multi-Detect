# Polaris 仿真图传、云台相机与录像回放

本章节由 `docs/polaris-sim.md` 链接，视频实现位于 `multidetect.polaris_sim.video`。网关进程可直接嵌入 `VideoService`，RTSP 默认监听 `0.0.0.0:8554`，路径固定为 `/v{vehicle_index}/cam` 与 `/v{vehicle_index}/gimbal`；例如 `rtsp://192.168.1.138:8554/v0/cam`。Polaris 现有视频入口接受普通 RTSP URL；这里不使用 `MultiDetectReplayRtsp` 的自定义 stdin 帧 wire，后者属于其单独的回放发布工具。

## 相机采集与编码

`GazeboTopicSource` 在 SITL 容器内用 `gz.transport13` 订阅 Image 主题，通过 `docker exec` stdout 传输带长度/尺寸/时钟信息的 BGR24 原始帧。帧缓冲最多保留一帧，编码落后时丢弃旧帧以限制延迟；源序号在 Gazebo 回调处递增，因此接收端能从序号缺口统计丢帧。默认主题与现有采集脚本一致：

```text
/world/{world}/model/{model}/link/camera_link/sensor/camera/image
```

`x500_mono_cam` 的固定相机和 `x500_gimbal` 的云台相机都使用该主题模式；具体 model 实例名由车辆启动器传入。`x500_gimbal` 模型当前公开的是安装在云台上的这一台 RGB 相机。若同一飞行器要同时发布独立的机身主相机和云台相机，需分别把真实存在的两个 Gazebo 主题传给两次 `VideoService.add_gazebo_camera(...)`；不要把同一主题映射成两个源后宣称为独立视角。视频服务不创建或合成相机传感器。

`VideoSettings` 默认输出 1280×720@30、2.5 Mbit/s、H.264 Baseline、GOP 1 秒。`profile` 可设为 `baseline` 或 `main`，分辨率、帧率、码率和每路 CPU 编码线程均可调。编码器固定使用 `/usr/bin/ffmpeg` 的 `libx264`，`ultrafast`/`zerolatency`、YUV420P、AUD 与重复 SPS/PPS；不调用 NVENC/CUDA。每路默认最多 2 个 x264 线程，6 路同时工作时最多 12 个编码线程。

独立运行单路采集服务的示例：

```bash
PYTHONPATH=src python scripts/polaris_sim/video.py \
  --vehicle-index 0 --camera gimbal --container md-smoke-rw-slot-5 \
  --world rw-example --model x500_gimbal_0
```

网关可注入同一实例的 netem shaper：

```python
video = VideoService("192.168.1.138", 8554, shaper=netem_shaper)
```

约定签名为 `shape(direction: str, payload: bytes) -> list[tuple[delay_s, payload]]`。本 lane 对发送到 GCS 的 RTP 包调用 `shape("down", packet)`；空列表表示丢包，正延迟由本服务的有序调度线程实施。调用沿用网关的 `NetemShaper` 实例，不重复保存或管理 netem 配置。RTSP 控制消息不做损伤，MAVLink 方向由网关 lane 处理。此 worktree 当前还没有合并网关 `NetemShaper`，测试使用同签名的假 shaper。

## 延迟测量

每个 H.264 access unit 会插入 user-data-unregistered SEI，包含源序号、Gazebo 仿真时间戳和相机回调处的 `time.monotonic_ns()`。测量客户端用 RTSP/TCP 解包 RTP、解析 SEI，并在本机同一 monotonic 时钟域内计算采集至完整帧到达延迟：

```bash
PYTHONPATH=src python scripts/polaris_sim/measure_video_latency.py \
  --host 192.168.1.138 --mount /v0/cam --duration 10 \
  --output /data/scratch/polaris-sim/video-latency.json
```

报告字段包含 p50/p95、序号缺口估算的掉帧率、RTP 缺包数和实际帧率。该测量脚本须在网关工作站运行；跨设备 monotonic epoch 不保证一致，跨设备测量需先具备同步时钟/偏移估计。零 netem 的目标为 p95 < 400 ms；本次受限环境没有可连接的 SITL/Docker，不能把合成帧结果冒充真实 Gazebo 实测。

## 云台画面

云台画面来自 Gazebo 的 `camera_link` RGB 主题，随物理关节角度变化；传输层不会另行旋转或裁剪图像。现有仿真模型把 `cgo3_camera_joint` 映射为 pitch、`cgo3_vertical_arm_joint` 映射为 yaw、`cgo3_horizontal_arm_joint` 映射为 roll。RTSP 只负责发布，MAVLink Gimbal Manager v2 指令仍由 Polaris/PX4 链路传递，AI 与视频进程不获得飞控控制权。

飞行器具备独立机身相机与云台相机时，网关分别配置 `cam` 与 `gimbal` 两个 source；设置不同的 `VideoSettings` 可使用不同分辨率/码率。物理指令前后画面差异必须在已租用 SITL slot 的真实 gimbal model 上验证；无模型或没有关节响应时应报告失败，不以静态 fixture 代替。

## 录像与回放

录像 CLI 通过网关控制面调用现有接口，写入 `/data/scratch/polaris-sim/recordings/<UTC 时间>/`：

```bash
PYTHONPATH=src python scripts/polaris_sim/record.py start
PYTHONPATH=src python scripts/polaris_sim/record.py stop
```

网关记录管理器使用 `RecordingSession`。视频以 RTSP/TCP 输入 `ffmpeg -c:v copy` remux 为 MKV，不重编码；MAVLink recorder 向网关 UDP 14550 发送 GCS heartbeat 注册临时 UDP 源端口，然后将收到的 MAVLink v1/v2 帧写成带 Unix 微秒时间戳的 tlog。录制管理器可订阅 AI 网关 SSE `/v1/events` 写 `ai-events.jsonl`；不可用时在 manifest 中写明，不伪造事件。Gateway lane 需要在 `/v1/record/start|stop` handler 中持有 `RecordingSession`，start body 可带 `include_ai_events`，stop 返回最终 manifest。

`manifest.json` 包含车辆 slot/sysid/world、起止 UTC、每个视频/tlog/事件文件的字节数与 SHA-256；AI 事件状态会标明已采集、不可用或未配置。回放加载时会校验路径、文件长度与哈希。回放命令：

```bash
PYTHONPATH=src python scripts/polaris_sim/replay.py \
  /data/scratch/polaris-sim/recordings/<UTC 时间> --speed 1 --loop
```

回放 RTSP 保持 H.264 码流并注入当前播放的延迟 SEI；tlog 按原始记录时间轴发送，速度仅接受 `0.5`、`1`、`2`，`--loop` 循环播放。默认占用 RTSP 8554 与 UDP 14550，因此回放时应停止实时网关，或显式指定空闲端口，并让 Polaris 指向回放端口。MAVLink 出站仅在收到 GCS heartbeat 后开始单播注册客户端，保留原始 sysid。

录制需要仓库已有 `pixhawk` extra 中的 `pymavlink`；图传和 RTSP 服务不新增 Python 依赖。运行环境还需系统 `/usr/bin/ffmpeg` 与 `/usr/bin/ffprobe`。长时间运行的网关/录像服务按 workstation 规则由 `systemd-run --unit=psim-... --collect --property=MemoryMax=...` 管理。

## 验证边界

`tests/polaris_sim/test_video.py` 覆盖原始帧/SEI、Annex-B 与 RTP 标记、RTSP TCP interleaved/UDP、netem 丢包、libx264 参数、端到端本机测量、MKV/tlog manifest 到 RTSP/MAVLink 回放。测试不启动 Polaris 二进制。Gazebo 相机主题存在性、Polaris Android 播放、真实云台指令前后画面差异、SITL 的 10 秒 ffprobe smoke 与真实局域网 p95 仍需在获得 SITL slot 和 Docker/GPU 权限后完成。
