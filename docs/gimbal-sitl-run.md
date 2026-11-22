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
