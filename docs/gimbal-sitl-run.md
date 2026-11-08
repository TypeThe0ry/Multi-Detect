# x500 云台机型 SITL

`x500_gimbal` 是可选的 PX4 Gazebo Harmonic 机型。档案使用 PX4 autostart 4019、`PX4_SIM_MODEL=gz_x500_gimbal`，并把 RGB 图像绑定到 `camera_link` 上的 `camera` 传感器。普通 suite 仍使用原来的机型和主题。PX4 当前机型说明列出的云台范围为 roll ±45°、pitch -135° 至 +45°、yaw 可连续旋转；profile 以 ±180° 表示 yaw 范围。

运行 R21IDR3/R24Plan 的三个云台任务：

```bash
export PYTHONPATH=src:.
export PYTHONUTF8=1
export TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/run_nightly_regression.py \
  --model-set r21idr3-r24plan --suites gimbal
```

仓库的 `simulation/docker/gpu/Dockerfile` 只以 `px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8` 为基底并安装 EGL；它没有复制 PX4 模型。此工作没有启动 Docker，因此无法核实该 digest 的实际文件系统中是否已经包含 `x500_gimbal` 模型目录和 4019 启动配置。先在允许的镜像检查环境确认以下两个路径：

```text
/opt/px4-gazebo/share/gz/models/x500_gimbal/model.sdf
/opt/px4-gazebo/etc/init.d-posix/airframes/4019_gz_x500_gimbal
```

如果任一路径缺失，应在 PX4 SITL 镜像构建阶段从**与基础镜像完全相同的 PX4 revision**复制模型及其依赖，而不是从不同版本混入文件。将模型目录放在 Docker build context 的 `px4-gz-models/x500_gimbal/`，在 `simulation/docker/gpu/Dockerfile` 的 EGL 安装层之后加入：

```dockerfile
COPY px4-gz-models/x500_gimbal/ /opt/px4-gazebo/share/gz/models/x500_gimbal/
COPY px4-gz-models/gimbal/ /opt/px4-gazebo/share/gz/models/gimbal/
RUN test -s /opt/px4-gazebo/share/gz/models/x500_gimbal/model.sdf \
    && test -s /opt/px4-gazebo/share/gz/models/gimbal/model.sdf \
    && test -f /opt/px4-gazebo/etc/init.d-posix/airframes/4019_gz_x500_gimbal
```

若基础镜像也缺少 `4019_gz_x500_gimbal`，仅复制 SDF 不够：构建该基础镜像時需從同一 PX4 revision 重新编译/安装包含 `gz_x500_gimbal` airframe 的 PX4 SITL build，并将生成的 `etc/init.d-posix/airframes/4019_gz_x500_gimbal` 一并安装到上述路径。`x500_gimbal` 的 SDF 还引用 gimbal 子模型/插件时，必须把这些引用的模型和插件一并放进对应模型资源目录并保留原有相对布局。

镜像构建和实际 SITL 飞行不属于本次验证范围。相关 Python 单测通过 fake 校验生成的启动环境、主题和 MAVLink 消息间隔请求。
