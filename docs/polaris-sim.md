# Polaris 仿真接入

本目录提供 Polaris 地面站到本机 PX4/Gazebo SITL 的 LAN gateway。slot 5/6/7 分别映射 sysid 1/2/3；MAVLink TCP 5770 是 Android 默认链路，多车共用一条 TCP 连接。桌面端可选 UDP 14550。Gateway 不修改 PX4 固件参数；SITL 容器沿用 `scripts/run_simulator_docker.py` 的资产校验、启动补丁和 Docker 网络规划。

图传与录像细节见 [`polaris-sim-video.md`](polaris-sim-video.md)，AI 查询与输入数据契约见 [`polaris-sim-ai.md`](polaris-sim-ai.md)。

实际世界由 `scripts/realworld/make_world.py <rw_site> --weather <weather>` 生成。默认启动会从 `simulation/gazebo/worlds/rw_*__<weather>.sdf` 选择已生成的世界；没有 SDF 时，`up.sh --dry-run` 和启动错误会打印可直接执行的生成命令。真实地点、天气和机型场景清单见 [`realworld-scenes.md`](realworld-scenes.md)。

## 端口

| 端口 | 协议 | 方向/用途 |
|---:|---|---|
| 5770 | TCP | Polaris Android 到 Gateway；一条连接承载多个 sysid |
| 14550 | UDP | Polaris 桌面链路；收到 GCS heartbeat 后单播遥测到该地址 |
| 8780 | TCP/HTTP | health、车辆、netem、录像、自检和发现信息 |
| 8781 | UDP multicast | 发送到 `239.255.77.77:8781` 的服务发现 JSON |
| 8554 | TCP/RTSP | video lane 的 H.264 流入口 |
| 8790 | TCP/HTTP | AI gateway 入口，由 AI lane 提供 |
| `14560 + 10×slot` | UDP | PX4 控制输入；Docker 只发布到 host loopback，Gateway 回送命令 |
| `14561 + 10×slot` | UDP | PX4 MAVLink 遥测输出；Gateway 在宿主机接收，例 slot 5 为 14611 |

控制/遥测端口对是 SITL 容器现有的端口族。遥测端口是容器向 Docker host gateway 发出的目标端口，不通过 Docker `--publish` 映射；Gateway 在其宿主机端口接收。不要将内部端口 14560/14561 与对 Polaris 的 14550 混用。

## 网关

在仓库环境安装项目并运行：

```bash
python -m pip install -e '.[pixhawk]'
polaris-sim-gateway --slots 5,6 --world rw_suburb_davis --airframe x500_gimbal
```

`pymavlink` 已是项目 `pixhawk` optional extra 的依赖；网关转发核心使用 Python 标准库，gimbal/飞行烟测用该 extra。监听默认是 `0.0.0.0`，但 TCP/HTTP/GCS peer 只接受私有 LAN 或 loopback 地址。若设置 `POLARIS_SIM_TOKEN`，HTTP 写操作需带 `Authorization: Bearer <token>`；未设置时只允许私有 LAN 地址。网关不会修改 ufw/iptables。

标准容器名下的车辆相机由 Gateway CLI 自动配置；生命周期脚本会显式传入相机容器和 Gazebo 模型实例。需要手动覆盖时可重复传入 `--video-source SLOT CAMERA CONTAINER WORLD MODEL`，仅调试 MAVLink 时可传 `--no-video`。

车辆按 slot 固定 sysid：5→1、6→2、7→3。TCP GCS 在连接建立后接收遥测；GCS heartbeat 会广播给所有 SITL。UDP GCS 必须先发 heartbeat 才会注册，遥测仅回送到已注册的 UDP 地址。上行 MAVLink 根据 `target_system` 单独路由；target 0 和 GCS heartbeat 才广播。未知目标不会发给任意车辆。TCP 遥测队列有界，拥塞时淘汰最旧的遥测包，不阻塞 SITL UDP 接收。

`COMMAND_LONG`/`COMMAND_INT` 和 Gimbal Manager v2 命令可透传；`GIMBAL_MANAGER_SET_ATTITUDE`、`GIMBAL_MANAGER_SET_PITCHYAW`、`GIMBAL_MANAGER_SET_MANUAL_CONTROL` 按目标 sysid 路由，`GIMBAL_DEVICE_ATTITUDE_STATUS` 随车辆遥测回到 GCS。云台真实反馈烟测：

```bash
python scripts/polaris_sim/gimbal_smoke.py --host 192.168.1.138 --sysid 1
```

命令会使用 `MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE` (1001) 获取控制权，再发 `MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW` (1000)，并要求收到 `COMMAND_ACK` 与 `GIMBAL_DEVICE_ATTITUDE_STATUS`。这只验证 MAVLink 路由和设备反馈，不替代视觉/云台角度精度验收。

## 一键启动

```bash
scripts/polaris_sim/up.sh --world rw_suburb_davis --weather clear_noon \
  --vehicles 2 --airframe x500_gimbal --gpu 2 --camera-profile lite
scripts/polaris_sim/down.sh
```

可省略 `--world` 以自动选择匹配天气且已生成 SDF 的世界。当前 GPU 调度只允许 GPU 2，最多运行 slot 5/6 两个容器；每个容器显存预算为 4 GiB，单次运行不超过 1 小时。启动前检查 GPU 2 显存，若已用超过 17 GiB，不启动新容器；不得使用 GPU 0/1/3。`--gpu none` 使用本机已存在的 CPU 镜像。启动只使用本机现有镜像，不会拉取镜像；优先检查 `multidetect/px4-sitl-gazebo:gz-rendering-830-r1`。SITL、含嵌入视频服务的 Gateway、AI 服务和持锁 session 由 transient `systemd-run` unit 管理。

slot 7 已被占用且请求 3 架时，会只租 slot 5/6 并在启动报告中注明；slot 5/6 不可用时启动失败，不等待或清理其他任务。所有锁持有在 session unit 退出时释放。状态和录像位于 `/data/scratch/polaris-sim/`。`down.sh` 只停止该状态文件登记的 Polaris unit。视频/AI入口未出现时会跳过并提示，不会伪造服务就绪。

dry-run 不租 SITL 锁、不起 Docker、不写状态：

```bash
scripts/polaris_sim/up.sh --world rw_suburb_davis --vehicles 2 --gpu auto --dry-run
```

就绪后终端会给出 TCP/UDP 地址、RTSP/AI 地址，以及可编码为二维码的 `polaris-sim://...` 文本。应用仍可手动输入 `192.168.1.138:5770`，或通过 discovery 配置。

`--camera-profile` 控制 Gazebo 相机模型覆盖与 RTSP 默认编码参数，默认 `lite`。覆盖模型从所选本地镜像的 `mono_cam/model.sdf` 生成到 `/data/scratch/polaris-sim/overlay/<profile>/models/mono_cam/model.sdf`，以只读挂载并放在 `GZ_SIM_RESOURCE_PATH` 首位；仓库中的训练模型不会被修改。HD 保留镜像原模型。

| Profile | Gazebo 相机 | RTSP 默认值 | 取舍 |
|---|---:|---:|---|
| `lite` | 640×480 @ 10 fps | 640×480 @ 10 fps | 默认；渲染开销最低 |
| `standard` | 960×720 @ 15 fps | 960×720 @ 15 fps | 清晰度与开销折中 |
| `hd` | 1280×960 @ 30 fps | 1280×720 @ 20 fps | 保留原始渲染，内存增长风险最高 |

profile 和 RTSP `video_settings` 会写入 `state.json`、`GET /v1/vehicles`。RTSP 编码输出不会超过对应 profile 的相机分辨率或帧率。

### 已知问题：gz-sim 相机渲染内存增长

联调观察到 `x500_mono_cam` 容器内的 gz-sim 内存随相机渲染像素率线性增长；是否有订阅者、使用哪个世界或是否启用深度相机都没有改变该现象。已测样例：1280×960 @ 30 fps 约 110 MB/s，640×480 @ 10 fps 约 9 MB/s。slot 7 的 `rc_cessna` 长任务曾稳定在 8.4 GiB，原因尚未确定。

Docker 容器内存上限由 `POLARIS_SIM_CONTAINER_MEMORY` 设置，默认 `16g`。超过上限时 Docker OOM 会杀掉该仿真容器，不会停止其他 slot 或工作负载。生命周期还会启动 `psim-memwatch`；任一自有 SITL 容器达到上限的 90% 时，看门狗只停止对应容器并把原因写入 `state.json`。`down.sh` 会一并停止看门狗。可用下列命令观察占用：

```bash
docker stats md-polaris-sim-slot-5 md-polaris-sim-slot-6
curl http://127.0.0.1:8780/v1/doctor
```

Doctor 在超过 10 GiB 时报告 WARN，超过 14 GiB 时报告 FAIL，并建议重启仿真；这些阈值是诊断提示，看门狗仍使用配置上限的 90%。

## Netem 与录像

`NetemShaper` 位于 `multidetect.polaris_sim.gateway.netem`。Gateway 在同一进程内嵌入 RTSP `VideoService`，MAVLink 与 RTP 共用一个 shaper 实例；RTSP 对每个下行 RTP 包调用 `shape("down", packet)`，返回 `None` 表示丢包，返回秒数表示排队延迟。`up` 表示 GCS→SITL，`down` 表示 SITL→GCS。带宽单位为 kbps，0 表示不限速；抖动是均匀分布的 ±毫秒。两方向参数和随机流独立，同一 seed 与包顺序可复现。

读取/设置示例：

```bash
curl http://127.0.0.1:8780/v1/netem
curl -X PUT http://127.0.0.1:8780/v1/netem \
  -H 'Content-Type: application/json' \
  -d '{"seed":42,"up":{"delay_ms":80,"jitter_ms":20,"loss_pct":2,"bandwidth_kbps":256},"down":{"delay_ms":120,"jitter_ms":30,"loss_pct":1,"bandwidth_kbps":1024}}'
curl -X POST http://127.0.0.1:8780/v1/record/start
curl -X POST http://127.0.0.1:8780/v1/record/stop
```

录像是 `/data/scratch/polaris-sim/recordings/*.jsonl`，每行含墙钟时间、方向、sysid 和 Base64 MAVLink 数据。`POST /v1/record/replay` 接受 `{"recording_id":"<basename>.jsonl","speed":1.0}`，只向已连接 GCS 重放下行遥测，不把历史控制命令回注 PX4。H.264/RTP 录像与视频回放由 video lane 实现并复用 `NetemShaper`；本 Gateway 不声称生成视频录像。

## 自检与端到端验证

```bash
python scripts/polaris_sim/doctor.py --host 192.168.1.138
python scripts/polaris_sim/doctor.py --host 192.168.1.138 --json
python scripts/polaris_sim/smoke.py --host 192.168.1.138 --sysid 1
```

`GET /v1/doctor` 和 CLI 检查 TCP 监听、ufw/iptables 只读规则、LAN IP、心跳、GPS fix、RTF、RTSP/ffprobe 与 AI HTTP。防火墙只有精确端口的放行规则且来源限制在本机 LAN 子网时才报告 PASS；缺少规则时只输出限制到检测到 LAN CIDR 的 `sudo ufw allow from <LAN_CIDR> ...` 命令，不改防火墙。假 GCS/假 SITL 集成测试不需要 Polaris 二进制；它们在进程内验证两车按 sysid 隔离上行、共用遥测队列、HTTP handler、发现和记录流程。

真实 SITL smoke 会通过 slot 5 租约运行，向 sysid 1 解锁、爬升至 20 m、悬停后降落，保存 ACK 延迟和位置采样到 scratch。它需要当前工作站 Docker、NVIDIA 驱动、可用 slot 5、本机镜像及已生成真实世界 SDF；这些资源未就绪时不能把单元测试等同于飞行通过。

## 故障排查

| 现象 | 检查 | 处理 |
|---|---|---|
| Android 连不上 TCP | `ss -ltnp | grep 5770`、`doctor.py --json` | 确认 Gateway unit 运行；按 doctor 建议检查 LAN 防火墙 |
| 多车只见一架或 sysid 错 | `GET /v1/vehicles`、`docker ps` | 核对 slot 5/6/7、`14561 + 10×slot` 遥测端口和场景容器 sysid |
| 心跳有但 GPS 未 fix | `/v1/doctor` 的 GPS 检查 | 等待 SITL GPS_RAW_INT fix type ≥3；不要在无 fix 时起飞 |
| 场景 SDF 缺失 | 检查 `simulation/gazebo/worlds/rw_*__*.sdf` | 执行错误信息给出的 `make_world.py` 命令 |
| RTSP 可连但无 H.264 | `ffprobe -rtsp_transport tcp rtsp://<host>:8554/v0/cam` | 检查 video lane 相机主题和 RTP 转发；编码应为 CPU x264 |
| gimbal ACK 超时 | `gimbal_smoke.py`、`GIMBAL_DEVICE_ATTITUDE_STATUS` | 核对 x500_gimbal 资产/profile 和 PX4 云台管理器状态，见 [`gimbal-sitl-run.md`](gimbal-sitl-run.md) |
| netem 延迟过大 | `GET /v1/netem`，检查 bandwidth 与包长 | 带宽上限会增加序列化队列时延；设 0 可关闭上限 |
| slot 7 无法租用 | `/data/scratch/multidetect-sitl.lock.slot-7/owner` | 保留现有任务；本工具的三车启动会降为 slot 5/6 |
