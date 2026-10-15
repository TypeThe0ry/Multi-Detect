# 真机机载运行骨架

这套入口让 Jetson Orin Nano 8GB 直接连接 PX4 和相机，运行原来的 `OnboardPolicy`、`OnboardLoop` 和 `MissionExecutor`。SIM 采集器仍使用自己的启动、采集和执行流程；真机入口使用新的传输接口，不复制任务决策逻辑。

当前是可测试的运行骨架，尚未完成 Orin、真实串口、CSI 相机和真机飞行验证。回放通过只能说明本机进程在给定输入和延迟下持续发送设定点，不能证明实际飞行性能。

## 接口和线程

- `VehicleInterface` 提供 `start()`、`snapshot()`、`send_setpoint()`、`arm()`、`set_mode()`、`land()`、`rtl()`、`close()`。状态包括本地 NED 位置、姿态、速度、home、落地状态、电池和各类接收时间。
- `PymavlinkVehicle` 支持串口和 pymavlink UDP 地址，复用 SIM 的 MAVLink 接收和设定点编码。位置单位为米，NED 的 down 向下为正；接口姿态和 yaw 使用度。
- `CameraSource` 提供 `start()`、`latest()`、`close()`。`CameraFrame` 包含编码后的图像、单调时钟时间和标定数据。
- 相机读取在独立线程内完成，只保存最新一帧。读取失败后保留旧帧的原始时间，不把旧图刷新成新图。
- 设定点线程默认 20 Hz，配置不得低于 10 Hz。它独立于推理线程，连续发送当前安全目标；不因一次 1 至 2 秒的慢模型调用而停止发送。
- 同时最多运行一次任务决策。调用决策前，把设定点改为当前悬停位置；慢调用等待期间继续悬停，返回的动作由原有任务执行器决定。

普通 OpenCV 相机时间是本机收到图像的时间，并非曝光硬件时间。它能发现读取中断，不能识别驱动持续返回重复图像的情况，也没有完成相机和飞控的硬件时间同步。

## Orin 上需要的本地文件

使用已经准备好的 Python 环境，包含项目依赖、pymavlink、串口支持、OpenCV、NumPy，以及现有策略需要的本地推理依赖。CSI 使用的 OpenCV 必须带 GStreamer 支持，系统必须已有对应的相机和 Argus 驱动。本说明不执行安装或下载。

真机的 `--policy-config` 必填。配置中的四个资源必须已经存在；相对路径按仓库根目录解释。Linux 上的 `llama_server` 应指向 Orin 上可执行的本地程序，不能沿用 SIM 环境的 Windows `.exe` 默认路径。

```json
{
  "yoloe_weights": "/opt/multidetect/models/yoloe/best.pt",
  "gguf": "/opt/multidetect/models/slow-system.gguf",
  "mmproj": "/opt/multidetect/models/mmproj.gguf",
  "llama_server": "/opt/multidetect/bin/llama-server",
  "mission": "find"
}
```

路径是填写方式示例，不代表这些文件已经部署。入口会检查文件是否存在，缺少资源时报错，不自动下载。`find` 使用现有查找任务；其它任务选项仍由 `scripts/onboard_policy.py` 解释。配置中的 `task_text` 如果存在，会按现有策略规则覆盖命令行任务文字。

## 串口和 PX4

UART 可以使用 `/dev/ttyTHS1` 一类设备名，USB 可以使用 `/dev/ttyACM0` 或稳定的 `/dev/serial/by-id/...` 路径。实际名称取决于板卡、连接和系统配置，示例名称不是自动探测结果。启动进程的用户需要已有串口访问权限。飞控端口的 MAVLink 配置、串口波特率和电气连接应与伴随计算机一致。

串口入口默认波特率为 `921600`，也可以用 `--baud` 指定已有配置的波特率。UDP 示例为 `--endpoint udpin:0.0.0.0:14540`；UDP 不使用串口波特率。程序通过飞控 heartbeat 识别目标，通过 MAVLink 请求必要的消息频率。它不写 PX4 参数。

启动需要连续收到新鲜的位置、姿态、heartbeat 和图像，并且真机必须提供 `HOME_POSITION` 的本地 NED 坐标。正常入口从已落地、未解锁状态启动：先发送悬停设定点，再请求 OFFBOARD 和解锁，确认飞控回传的状态后起飞。没有地面站参与此流程。

`--altitude` 是相对于 home 的起飞高度。例如 home 的本地 down 为 `d_home`，目标起飞 down 为 `d_home - altitude`。不能假定 PX4 的估计器原点就是起飞地面，也不能把绝对本地 down 直接当作离地高度。任务适配层把策略的高度原点换算到 home 平面，再把动作交回原来的本地 NED 坐标执行。

启动会等待 home 和新的落地状态消息到齐，不把消息到达顺序当作飞控故障。模型初始化期间 home 若改变，会在解锁前停止启动；解锁或任务期间 home 相对绑定值变化超过 0.5 米，会进入悬停和所选故障处理流程，避免策略和起飞使用不同坐标原点。

## 相机和标定

USB 相机使用 `--camera-backend v4l2 --camera /dev/video0`，也支持数字设备索引。程序向 V4L2 请求标定文件中的分辨率，并逐帧检查实际尺寸。驱动如果返回其它尺寸，会停止更新图像并触发后续看门狗；程序不会静默缩放图像或沿用不匹配的内参。

CSI 使用 `--camera-backend gstreamer`，把完整 pipeline 作为 `--camera` 的一个参数。下面是需要按实际传感器模式修改的示例：

```bash
python scripts/onboard_main.py real \
  --endpoint /dev/ttyTHS1 --baud 921600 \
  --camera-backend gstreamer \
  --camera 'nvarguscamerasrc sensor-id=0 ! video/x-raw(memory:NVMM),width=1280,height=720,format=NV12,framerate=30/1 ! nvvidconv ! video/x-raw,format=BGRx ! videoconvert ! video/x-raw,format=BGR ! appsink max-buffers=1 drop=true sync=false' \
  --calibration configs/local/orin-front-camera.json \
  --policy-config configs/local/orin-policy.json \
  --task 'Find the inspection object and record its position.' \
  --altitude 10 --failsafe land \
  --output tmp/onboard-runtime/real-report.json
```

`appsink max-buffers=1 drop=true sync=false` 用于限制缓存、丢弃过期图像。分辨率必须与标定文件一致；相机的实际传感器模式和驱动是否支持此 pipeline，需要在目标 Orin 上验证。

标定支持仓库已有的 `{"schema_version": 1, "calibration": {...}}` 文件形式。下面的数值仅演示格式，不能作为真机测量结果使用：

```json
{
  "schema_version": 1,
  "calibration": {
    "calibration_id": "replace-with-measured-camera-id",
    "width_px": 1280,
    "height_px": 720,
    "fx_px": 900.0,
    "fy_px": 900.0,
    "cx_px": 640.0,
    "cy_px": 360.0,
    "k1": 0.0,
    "k2": 0.0,
    "p1": 0.0,
    "p2": 0.0,
    "k3": 0.0,
    "mount_pitch_down_deg": 10.0,
    "mount_yaw_right_deg": 0.0,
    "mount_roll_clockwise_deg": 0.0,
    "camera_offset_forward_m": 0.18,
    "camera_offset_right_m": 0.0,
    "camera_offset_down_m": 0.12
  }
}
```

六个外参都必须明确填写。三个角分别表示相机向下俯视、向右偏航和顺时针滚转；三个偏移沿机体前、右、下方向，以米表示。应使用实测安装数据，不能直接拿 Gazebo 的标定文件用于真机。畸变使用五项 Brown-Conrady 系数 `k1,k2,p1,p2,k3`。

也支持 `{"intrinsics": {"width": ..., "height": ..., "fx": ..., "fy": ..., "cx": ..., "cy": ..., "distortion": [...]}, "extrinsics": {...}}` 的形式；外参名称与上面相同。

真机像素落地投影使用当前完整姿态、相机外参和畸变校正，但仍与一个水平地面相交。当前入口把地面设为 home 平面。使用前要确认实测地面与 home 平面的关系；坡地、屋顶或地面高差不能由这层模型自动恢复。当前共享飞行控制还假定相机朝前，入口要求 `mount_yaw_right_deg=0`、`mount_roll_clockwise_deg=0`；投影本身支持多轴安装，不代表控制策略已经支持任意安装。

USB 相机运行示例：

```bash
python scripts/onboard_main.py real \
  --endpoint /dev/serial/by-id/replace-with-flight-controller --baud 921600 \
  --camera-backend v4l2 --camera /dev/video0 \
  --calibration configs/local/orin-front-camera.json \
  --policy-config configs/local/orin-policy.json \
  --task 'Find the inspection object and record its position.' \
  --altitude 10 --stream-hz 20 --failsafe rtl \
  --shutdown-action land --completion-action land \
  --output tmp/onboard-runtime/real-report.json
```

`--startup-timeout` 默认 30 秒，`--takeoff-timeout` 默认 45 秒。真实任务默认持续运行，不需要保持地面站连接；如需限定本次进程时长，可以设置 `--duration`，到时按退出流程交接飞控。

## 看门狗和退出

默认时间配置如下，单位均为秒：

| 参数 | 默认值 | 检查内容 |
| --- | ---: | --- |
| `--camera-timeout` | 0.75 | 最近收到的相机帧 |
| `--pose-timeout` | 0.75 | 位置和姿态，各自检查 |
| `--heartbeat-timeout` | 3 | 飞控 heartbeat |
| `--hold-before-failsafe` | 3 | 检测异常后的悬停宽限时间 |
| `--autonomy-timeout` | 15 | 一次任务决策调用 |

位姿或图像过期时，运行时锁定异常，丢弃待执行的推理结果，发送最后可信位置的悬停目标。即使数据恢复，也不会自动继续旧任务。宽限时间后执行操作者明确选择的 `--failsafe hold|land|rtl`；真机必须填写此选项。`hold` 持续悬停，不等同于退出进程。没有新鲜位姿时，只能保持最后可信设定点，并不能保证实际位置仍可信。

任务执行还检查 OFFBOARD 意外退出、意外上锁和动作执行超时。慢调用返回过期结果后，不会恢复被看门狗取消的动作。发送 LAND/RTL 后，程序通过后续 heartbeat 确认飞控已接管；确认失败会记录错误并重试交接。

任务正常完成使用 `--completion-action land|rtl`，默认 `land`。收到 Ctrl-C、SIGTERM 或时长结束后使用 `--shutdown-action land|rtl`，默认 `land`。退出过程中先转为悬停目标并请求飞控接管，等待状态确认，再停止模型、相机、设定点线程和串口；未能确认交接会明确报错。进程断电、操作系统卡死或物理断链不属于这种可控退出。

断链策略是独立工作：本入口不配置 PX4 的 RC、数据链或 OFFBOARD 丢失参数，不把地面站在线当作运行条件，也不宣称 `--failsafe` 替代飞控自身的链路丢失策略。操作方仍需另外明确无人值守任务的链路丢失行为。

## 无硬件回放

真实采集记录使用以下目录形式：

```text
artifacts/datasets/onboard-loop/<run>/episodes/<episode>/
  episode.json
  steps.jsonl
  mavlink.jsonl
  rgb/*.png
```

`steps.jsonl` 读取 `obs_before`、`obs_after` 中的 `rgb_path`、`camera_info` 和帧接收时间；`mavlink.jsonl` 读取 MAVLink 消息字段和 `receive_monotonic_ns`。优先使用图像的 `frame_received_monotonic_ns`，再回退到观察或步骤的接收时间。回放从第一帧开始，之前的 MAVLink 用于建立初始状态，之后保持记录中的时间间隔。RGB 路径必须留在 episode 目录内。

```bash
python scripts/onboard_main.py replay \
  --episode artifacts/datasets/onboard-loop/<run>/episodes/<episode> \
  --duration 6 --stream-hz 20 \
  --detector-latency 0.07 --slow-latency 1.5 \
  --output tmp/onboard-runtime/replay-report.json
```

回放以 1 倍墙钟速度运行，使用同一套策略和任务执行器，只把检测器与慢模型换成可配置延迟的桩。默认检测器延迟 70 ms，慢模型延迟 1.5 秒，不加载模型，不运行 GPU、Gazebo 或 PX4。`ReplayVehicle` 只记录输出命令，不向串口或网络发送。录制轨迹也不会因为输出动作改变，所以这不是闭环飞行仿真。

采集器通常只在决策前后保存 RGB，因此真实记录可能有超过 0.75 秒的图像间隔。回放保留这些间隔，不重复制造新鲜帧；记录结束后，最后一帧和最后位姿的时间也不会继续刷新。这类回放触发看门狗是预期结果。只为隔离验证设定点调度时，可以明确增大 `--camera-timeout` 到大于记录中的最大帧间隔，并让 `--duration` 留在有效记录范围内。代价是降低对旧图像的敏感度，该参数不能未经验证直接用于真机。`--pose-timeout` 和 `--heartbeat-timeout` 同理，应对应记录实际消息间隔。

本工作树没有实际的 `artifacts/datasets/onboard-loop` 记录，因此当前不能给出真实记录的回放结果。可以显式生成小型合成输入，检查线程时序和命令记录；输出会标记 `synthetic: true`：

```bash
python scripts/onboard_main.py replay \
  --generate-fixture tmp/onboard-runtime/synthetic-episode \
  --duration 6 --stream-hz 20 \
  --detector-latency 0.07 --slow-latency 1.5 \
  --output tmp/onboard-runtime/synthetic-report.json
```

生成目录必须尚不存在。合成输入只是按采集器格式写入的图像和遥测，不是录制的 SIM 任务，不提供识别率、任务成功率或飞行质量证据。

报告中的 `setpoints.sent_count`、`setpoints.rate_hz`、`setpoints.max_gap_s` 来自设定点线程；`transport_timing.setpoint_count`、`transport_timing.setpoint_rate_hz`、`transport_timing.setpoint_max_gap_s` 来自回放车辆实际收到的调用。频率按相邻发送间隔计算，最大间隔是连续两次发送的最坏间隔。还应查看 `slow_calls`、`detector_calls`、`pending_poll_count`、`autonomy_steps`、`watchdog_reason`、`error` 和 `ok`，确认确实覆盖了慢调用等待，且没有用过期输入继续任务。

`timing_passed` 要求平均发送频率至少 10 Hz、最大相邻间隔小于 0.5 秒；`slow_call_exercised` 单独说明慢调用是否实际完成。原记录没有合成来源标记时，报告的 `synthetic` 为 `null`，不能据此推断输入一定来自真实飞行。第一帧之前的遥测保留原有年龄，不会被重新标记为刚收到。

## 本次合成回放计时

2026 年 10 月 15 日，在当前 Windows 工作树上执行了下面的 CPU 计时。输入是显式生成并保存为采集器格式的合成 episode；没有真实 SIM 录像，没有运行模型、GPU、模拟器或飞控硬件。

```bash
python scripts/onboard_main.py replay --generate-fixture tmp/onboard-runtime-bench/final-episode --duration 6 --detector-latency 0.07 --slow-latency 1.5 --work-dir tmp/onboard-runtime-bench/final-frames --output tmp/onboard-runtime-bench/result-final.json
```

| 指标 | 本次结果 |
| --- | ---: |
| 设定点数量 | 120 |
| 实际平均发送频率 | 19.9899 Hz |
| 最大相邻发送间隔 | 0.094 秒 |
| 等待任务决策期间的轮询次数 | 116 |
| 检测器调用次数 | 1 |
| 慢模型桩调用次数 | 1 |
| 已接收的任务动作数量 | 1 |

报告中 `watchdog_reason` 为空，`policy_worker_stopped` 为 `true`，`outbound_commands_transmitted` 为 `false`，`ok` 为 `true`。这些数据证明本次包含 70 ms 检测延迟和 1.5 秒慢调用时，设定点线程仍持续运行。它们只反映这台 Windows 主机的进程调度，不能作为 Orin 串口、真实模型耗时、真实录像任务或真机飞行验收结果。由于回放轨迹不响应输出动作，此次也没有完成整个任务。

## 仍来自 SIM 的假设和待验证部分

- 搜索和任务执行仍复用原有策略。SIM 可以从静态 SDF 读取障碍；真机入口使用空世界，没有实测障碍地图，也没有新的实时避障、地形跟随或避碰能力。
- 地面投影仍是 home 高度上的平面，不提供地形模型。深度输入当前为 `None`，CSI/V4L2 只接入 RGB。
- 相机必须朝前安装，控制层尚未支持非零偏航或滚转安装。采集时间是软件接收时间，尚未验证传感器与 MAVLink 的时间同步误差。
- home、位置和姿态依赖 PX4 估计器输出。没有新增 GNSS 健康度、估计器质量或飞行包线验证，也没有实现独立的断链政策。
- 对 Orin Nano 8GB 的实际模型内存、推理耗时、热降频、串口抖动和 CSI 驱动行为尚无硬件测试结果。延迟桩的计时不能代替这些测量。
- SIM 采集器的 Gazebo 帧服务器、SITL 启停、深度和场景真值采集继续只在 SIM 路径中使用。真机入口没有迁移这些依赖。

## 本地验证

以下检查只运行 CPU 上的接口、线程、看门狗和回放测试：

```bash
python -m pytest -q -p no:warnings tests/test_onboard_runtime_camera.py tests/test_onboard_runtime_replay.py tests/test_onboard_runtime_vehicle.py tests/test_onboard_runtime.py tests/test_onboard_runtime_policy.py tests/test_onboard_main.py tests/test_onboard_policy_plan.py tests/test_onboard_loop.py tests/test_mission_executor.py tests/test_sim_teacher_executor.py tests/test_sim_teacher_live_loop.py tests/test_sim_teacher_primitives.py --basetemp tmp/pytest-onboard-runtime
python -m ruff check src/multidetect/onboard_runtime scripts/onboard_main.py scripts/onboard_policy.py tests/test_onboard_runtime_camera.py tests/test_onboard_runtime_replay.py tests/test_onboard_runtime_vehicle.py tests/test_onboard_runtime.py tests/test_onboard_runtime_policy.py tests/test_onboard_main.py
```

本次上述 12 个测试文件共 131 项通过，Ruff 通过；其中包含原有策略、任务执行器和 SIM 执行路径的回归检查。
