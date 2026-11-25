# x500 云台机型 SITL

`x500_gimbal` 是可选的 PX4 Gazebo Harmonic 机型。档案使用 PX4 autostart 4019、`PX4_SIM_MODEL=gz_x500_gimbal`；RGB 相机位于 `gimbal/model.sdf` 的 `camera_link` 上，RTSP mount 为 `/v{index}/gimbal`。普通机型和 suite 继续使用各自的相机主题。模型范围为 roll ±45°、pitch -135° 至 +45°、yaw 可连续旋转。

运行 R21IDR3/R24Plan 的三个云台任务：

```bash
export PYTHONPATH=src:.
export PYTHONUTF8=1
export TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/run_nightly_regression.py \
  --model-set r21idr3-r24plan --suites gimbal
```

Polaris 生命周期为 `x500_gimbal` 选择本机的 `multidetect/px4-sitl-gazebo:px4-main-53afcc82-gimbalid-r1` 镜像。它需含下列模型、相机子模型和 PX4 airframe；检查只读本地镜像，不会拉取：

```text
/opt/px4-gazebo/share/gz/models/x500_gimbal/model.sdf
/opt/px4-gazebo/share/gz/models/gimbal/model.sdf
/opt/px4-gazebo/etc/init.d-posix/airframes/4019_gz_x500_gimbal
```

生命周期的 lite/standard/hd overlay 从镜像 `gimbal/model.sdf` 修改相机传感器类型、尺寸和帧率为 `rgbd_camera` 档位。云台模型目录含网格，启动时只替换 `gimbal/model.sdf` 文件，不覆盖同目录的 `meshes/` 或 PX4 解析模型依赖所用的资源根。

机型和世界场景均存在后，启动一架并运行 smoke：

```bash
scripts/polaris_sim/up.sh --world rw_suburb_davis --gpu 2 --vehicles 1 \
  --airframe x500_gimbal --camera-profile lite
python scripts/polaris_sim/gimbal_smoke.py --host 192.168.1.138 --sysid 1
python scripts/polaris_sim/e2e.py --slots 5 --world rw_suburb_davis \
  --airframe x500_gimbal --steps mavlink,video --no-up --no-down
scripts/polaris_sim/down.sh
```

在运行 e2e 前确认 gateway 的 `/gimbal` mount 已有 H.264 帧。MAVLink 测试验证配置/俯仰命令的 `COMMAND_ACK`，并轮询 `GIMBAL_DEVICE_ATTITUDE_STATUS` 直到 pitch/yaw 误差均 ≤4°；视频测试仅查询真实存在的 `/gimbal`，然后比较云台在 -25°/+15° 时的 96×72 灰度帧，均值绝对差需 >2/255。测试使用系统 ffmpeg/ffprobe，不需要 Polaris 二进制。

真实观察至少 3 分钟时，每 10 秒运行一次 `docker stats --no-stream md-polaris-sim-slot-5` 并保存原始输出；同一采样窗同时记录 GPU2 显存。若 GPU2 显存超过 17 GiB，立即通过该 state file 对应的 `down.sh` 停止本任务。结束后确认无自有 `md-polaris-sim-slot-*` 容器与 5770/8780/8554 监听。

本地镜像确认命令：

```bash
docker run --rm --pull=never --network none --entrypoint sh \
  multidetect/px4-sitl-gazebo:px4-main-53afcc82-gimbalid-r1 -c \
  'test -s /opt/px4-gazebo/share/gz/models/x500_gimbal/model.sdf && \
   test -s /opt/px4-gazebo/share/gz/models/gimbal/model.sdf && \
   test -s /opt/px4-gazebo/etc/init.d-posix/airframes/4019_gz_x500_gimbal'
```

## X1 云台跟踪评测说明（2026-11-22，仅 SIM）

X1 评测飞行的稳态指向统计取 t>=20 s：初版 5 s 瞬态窗口过短，实测稳定需 9-11 s。21 条件基线几何锁定率 100%，观察到 0 次 ACK 拒绝，稳态指向 P95 <= 0.67 度。评测飞行可选打开 200 ms 机体角速度预览（桥接跟踪位姿），默认关闭；首次复飞 1 s 后偏移峰值 16.7→7.9 度、最后一个 >2 度样本 9.2→6.7 s，其余复飞尚在运行，不能据此宣称整体改进。以上均为 SIM 结果，不是硬件、飞行或生产验收。

### 仅元数据请求 `GETMETA`（仅 SIM）

collector 的 frame server（`scripts/sim_teacher_frame_server.py`）除原 `GET <ns>`（返回 MDGZSIM3 + MDGZDEP1，字节不变）外，新增 `GETMETA <ns>`：语义同样是“时间戳严格大于参数之前的第一帧”，但只回一条无像素的 `MDGZMET1` 记录（小端：8 字节魔数、u32 JSON 正文长度、JSON 正文、32 字节 SHA-256；正文含 stamp、相机信息 stamp、位姿、速度和实测 `joint_state`，实现见 `scripts/gazebo_meta_wire.py`）。`GETMETA` 由独立线程应答并自带序号，不会被正在编码的像素请求拖慢，两种记录整条写入、可交错。读端 `SensorStreamReader.request_meta()` 把结果放到独立的 `latest_meta`，不覆盖像素 `latest`；`X1TrackCapture` 在 `meta_supported` 时走该路径并保持一个请求在途，否则回落到原像素路径。
兼容规则：新 server 不改变旧 `GET`；旧 server 不认识 `GETMETA` 并会退出，所以读端默认不发送，只有 collector 在使用本仓库同版本的 bind-mount `scripts/` 时才开启（`MULTIDETECT_SIM_META_REQUESTS=0` 可强制回到像素路径）。容器内 `scripts/` 必须同时有 `sim_teacher_frame_server.py` 和 `gazebo_meta_wire.py`。仅 CPU 测试验证，未做实机或实飞验证。

## X1 固定翼 `fw_legs` 云台评测（2026-11-25，仅 SIM）

`fw_legs` 是 X1 云台评测队列中的显式可选项，用于测量固定翼各航段的指向和交接行为。评测按 `cruise`、`turn`、`hover` 和 `horizon` 分段统计。门限为：`turn` 指向误差 P95 ≤ 10°，`cruise` 指向误差 P95 ≤ 5°，固定翼锁定率 ≥ 0.9，可达覆盖率 ≥ 0.8，`unwind` 占比 ≤ 0.1；地平线验收为 P95 ≤ 4°、最大值 ≤ 8°、`degraded` ≤ 5%。这些指标是仿真记录上的几何和控制指标，不代表视觉检测锁定、硬件性能或生产验收。

控制链按以下 PR 合入并在评测中使用：

- #34：按固定翼航段选择可达目标，固定翼跟踪段不触发偏航回中。
- #35：跟踪目标采用 point-first 指向优先解。
- #36：修正固定翼交还悬停时的 `hover_preview`。
- #37：桥接 lock 不再因整圈 yaw 跳变破坏连续地平线，使用 `horizon4` 镜像。
- #39：悬停预瞄后先稳定，再进入计分保持段。
- #42：按最小 LOS slew 选择相邻航段目标，并使用盘旋模型。
- #43：加入末航段结束前预锁 A 和飞越平分线预瞄 B。
- #44：加入 ingress 预瞄 C 和限位感知解 D。
- #45：将默认候选镜像切到 `gimbal-horizon4`。
- #46：引导盘旋退出按 ETA 提前进入 lock。
- #47：修复云台命令 ACK 竞态，并加入 opt-in 的 `fw_min_leg_m` 短航段合并。

### 评测队列

队列配置为 `configs/simulation/x1-gimbal-eval.json`。`enabled` 控制队列是否可调度，`gimbal_eval` 开启云台评测，`fw_legs` 开启固定翼航段评测，`fw_min_leg_m` 可选地合并短于该距离的航段，`only` 过滤条件，`tag` 标识本轮，`image` 覆盖候选镜像，`track_preview_s` 与 `track_preview_max_deg` 控制可选的轨迹预览。配置中也可使用对应的 `track_preview_*` 键覆盖默认预览参数。固定翼评测分为 lane `a` 和 `b`，分别使用 slot 5 和 slot 6。

运行前只做 CPU 预检和队列检查，不能绕过 X1 lock。运行器使用 `md-x1-gimbal-eval-a.service` 或 `md-x1-gimbal-eval-b.service`，通过 lane 锁和共享 X1 flight lock 防止重复执行。GPU 准入仍由现有检查负责，`x1-allow-sunshine-compute` 与 `x1-allow-fluidx3d-share` 是显式 opt-in 标记，存在时才允许相应的共享场景，RTF 仍需通过评测门。不要在本固定翼评测中启动 Docker、SITL 或 GPU 作业，本页的 CPU 预检示例如下：

```bash
export PYTHONPATH=src:.
export PYTHONUTF8=1
export TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/realworld/x1_collect_next.py --gimbal-eval \
  gimbal-eval-cpu-preflight --image-id gimbal-horizon4
```

实际队列由 `scripts/realworld/x1_collect_next.py --gimbal-eval --lane a|b` 进入。`md-x1-resume` 仍是既有 X1 恢复流水线，固定翼评测使用独立的 `x1-gimbal-eval` 输出与收据，不应把恢复批次的结果混入本队列。

### 结果和局限（仅 SIM）

R1（巡航 P95 114°，回正占比 0.31）经上述修复链，到 2026-11-24 夜至 25 日的稳定性复飞（r9、r10a–e，Iowa 另补飞）：七个站点（Iowa、Amsterdam、Berlin、Shenzhen、Zurich、Rotterdam、Singapore）的有效飞行中，转弯 P95 为 2.2–6.2°（门限 10°），巡航 P95 ≤ 0.35°，可达覆盖 100%，地平线 max ≤ 1.9°；#46 之后 Iowa 的 10 次有效飞行全部通过（修复前每次约 25% 概率踩中回切地平线瞬态）。证据在 NAS：`/mnt/nas/AdminHome/volatic/x1-gimbal-eval/`（FW-GIMBAL-R3C.md 及各站 `collection/` 收据）。

局限：每站有效飞行 4–6 次，同版本运行间波动较大（例如 Shenzhen 同一版本出现过转弯 P95 17.3° 与 4.4°），不能据此给出通过概率；门限未放宽、未引入过渡窗口或样本剔除；只覆盖 SIM，不能推断实机、实飞、视觉检测或发布状态。飞行侧的偶发失败（固定翼巡航未按要求经过航点、云台自检 ACK 超时）单独统计，与云台控制无关。
