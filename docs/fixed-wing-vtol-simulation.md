# 固定翼与 VTOL 单目相机仿真

本页描述仓库内可以直接交给 PX4/Gazebo 栈的固定翼和 VTOL 资产。模型只组合 PX4 镜像中的原生机体和 `model://mono_cam`，所以气动、执行器和 PX4 话题仍由上游模型负责；仓库只负责稳定的模型名、RGB/深度相机安装和可选的分割相机 overlay。

## 机型清单

| 仓库模型 | PX4 airframe | `PX4_SYS_AUTOSTART` | `PX4_SIM_MODEL` | 主要场景 |
| --- | --- | ---: | --- | --- |
| `rc_cessna_mono_cam` | `4003_gz_rc_cessna` | 4003 | `gz_rc_cessna_mono_cam` | 既有固定翼兼容路径 |
| `advanced_plane_mono_cam` | `4008_gz_advanced_plane` | 4008 | `gz_advanced_plane_mono_cam` | 手抛/弹射研究、滑跑 |
| `standard_vtol_mono_cam` | `4004_gz_standard_vtol` | 4004 | `gz_standard_vtol_mono_cam` | 4+1 |
| `quadtailsitter_mono_cam` | `4018_gz_quadtailsitter` | 4018 | `gz_quadtailsitter_mono_cam` | tailsitter |
| `tiltrotor_mono_cam` | `4020_gz_tiltrotor` | 4020 | `gz_tiltrotor_mono_cam` | 通用 tiltrotor |

`x500_mono_cam=4001` 以及旧的 `rc_cessna_mono_cam`、`standard_vtol_mono_cam` 映射保持不变。PX4 airframe 脚本的默认模型名和 autostart 证据保存在 [`docs/reference/px4-pinned-fw-vtol-airframes.txt`](reference/px4-pinned-fw-vtol-airframes.txt)。

## 相机安装和光轴

Gazebo `mono_cam` 的相机坐标约定为沿相机 `+X` 看。固定翼新模型和 tiltrotor 的安装是机体前方、向下 25 度：

```
u_fw = (cos(25°), 0, -sin(25°)) = (0.9063, 0, -0.4226)
```

`standard_vtol_mono_cam` 继续使用已有的 35 度下俯安装；`rc_cessna_mono_cam` 保留原来 0 度前视安装，避免改变既有行为。新固定翼 `advanced_plane_mono_cam` 提供需要下视的相机基准。

`quadtailsitter_mono_cam` 使用机腹挂点，机体姿态为 `pitch=45°、yaw=180°`，因此平飞时光轴为后向下视：

```
u_body = (-cos(45°), 0, -sin(45°)) = (-0.7071, 0, -0.7071)
```

tailsitter 悬停时机头上仰 90 度。采用 ENU 中的 `R_y(-90°)`（机体 `+X` 指向世界 `+Z`）后：

```
u_hover = R_y(-90°) u_body = (0.7071, 0, -0.7071)
```

所以同一刚性安装在平飞是机腹下视，在悬停是世界前方下视；不在运行时动态改传感器 pose。深度相机与 RGB 同位，分辨率为 1280x960、`R_FLOAT32`、30 Hz。每个新模型都有对应的 `model-overlays/<model>_seg`，额外发布 `/multidetect/teacher/segmentation` 的 instance segmentation，相机内参和光轴与 RGB 一致。

## 起飞方式在 SITL 中的含义

新场景的 `spawn_id` 明确写出 `advanced_plane_runway_400m`、`quadtailsitter_vtol_hover` 和 `tiltrotor_vtol_hover`；出生点和朝向全部由场景 runtime 的 `spawn_pose` 提供，固定翼跑道为 450 m，满足至少 400 m 的滑跑空地。

滑跑使用 PX4 固定翼 runway takeoff。`4003` 和 `4008` airframe 脚本都设置了 `RWTO_TKOFF 1`，因此 SITL 可以在地面沿跑道加速、抬轮并进入爬升。4+1、tailsitter 和 tiltrotor 场景从地面高度的 VTOL 姿态启动，转换阶段由各自 PX4 airframe 参数负责。

手抛/弹射需要区分 PX4 参数和 Gazebo 物理：固定翼参数元数据中的 `FW_LAUN_DETCN_ON` 明确写着“基于测得加速度的自动 launch detection”，并注明用于 hand/catapult 且“不兼容 runway takeoff”；`FW_LAUN_MOT_DEL` 控制检测后的电机延迟。仓库没有把一个静止出生点伪装成已经发生的加速度事件，因此当前 dry-run 场景使用 runway/VTOL 标识。真实验证手抛/弹射时，主线程应在 Gazebo 中通过外部施加初速度/短时推力触发加速度，再设置 `FW_LAUN_DETCN_ON=1`；若镜像或桥接版本不转发该加速度，替代方案是保持 launch detection 关闭，使用 runway takeoff 或在 PX4 控制台显式执行起飞任务。`COM_THROW_EN`/`COM_THROW_SPEED` 是多旋翼 throw-start 参数，不是固定翼 hand-launch 的替代品。

## 静态 dry-run

Windows PowerShell 先执行：

```powershell
$env:TEMP=$env:TMP=$env:TMPDIR="D:\Projects\mdwt\af-sim\.tmp"
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
$env:CUDA_VISIBLE_DEVICES="-1"
$env:PYTHONPATH="src"
```

为每个场景打印 Docker 命令（不会启动 Docker）：

```powershell
python scripts/run_simulator_docker.py --scenario configs/scenarios/airframes/advanced_plane_mono_cam_001.scenario.json --job-dir .tmp/advanced-plane --container-name md-advanced-plane --out .tmp/advanced-plane/docker-plan.json
python scripts/run_simulator_docker.py --scenario configs/scenarios/airframes/quadtailsitter_mono_cam_001.scenario.json --job-dir .tmp/quadtailsitter --container-name md-quadtailsitter --out .tmp/quadtailsitter/docker-plan.json
python scripts/run_simulator_docker.py --scenario configs/scenarios/airframes/tiltrotor_mono_cam_001.scenario.json --job-dir .tmp/tiltrotor --container-name md-tiltrotor --out .tmp/tiltrotor/docker-plan.json
```

## 主线程真实启动验证

本会话只做静态和 dry-run。主线程在装有 Docker Desktop、PX4 镜像和 Gazebo 的环境中，先执行上面的环境变量设置，然后逐个运行：

```powershell
python scripts/run_simulator_docker.py --strict --scenario configs/scenarios/airframes/advanced_plane_mono_cam_001.scenario.json --job-dir .tmp/advanced-plane --container-name md-advanced-plane
python scripts/run_simulator_docker.py --strict --scenario configs/scenarios/airframes/quadtailsitter_mono_cam_001.scenario.json --job-dir .tmp/quadtailsitter --container-name md-quadtailsitter
python scripts/run_simulator_docker.py --strict --scenario configs/scenarios/airframes/tiltrotor_mono_cam_001.scenario.json --job-dir .tmp/tiltrotor --container-name md-tiltrotor
```

启动后应在容器日志中确认对应的 `PX4_SYS_AUTOSTART`、`PX4_SIM_MODEL`、RGB/深度话题和（启用 overlay 时）`/multidetect/teacher/segmentation`，结束后清理：

```powershell
docker stop --timeout 2 md-advanced-plane md-quadtailsitter md-tiltrotor
docker rm md-advanced-plane md-quadtailsitter md-tiltrotor
```
