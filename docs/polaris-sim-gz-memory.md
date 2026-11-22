# Gazebo RGB 相机内存增长实验

日期：2026-11-17（工作站本地时钟，实验文件名使用 UTC）

## 结论

`x500_mono_cam` 原始 `camera` 类型会持续增长：真实滨海湾 world、GPU2、16 GiB 容器上限、无视频订阅者时，`lite`（640×480@10）约 8.9 MB/s，HD（1280×960@30）约 109–113 MB/s。2026-11-17 的 leak2 复测把同一 RGB sensor 改为 `rgbd_camera`，保留 Gateway 视频订阅时输出彩色视频。最终 110 秒定时复测每 10 秒采样一次；Gazebo 初始化后容器 RAM 稳定在 5.504–5.509 GiB，Gazebo 进程显存稳定在 2,870 MiB，未观察到持续增长。彩色和深度 topic 均存在；视频桥使用原 `.../sensor/camera/image` topic，RTSP 是 H.264 640×480，抓帧的 U/V 色度均非中性。

在同一个 x500 模型中移除 `mono_cam` 的 RGB `<sensor type="camera">` 后，RSS 在约 159 秒内保持 5.107 GiB 不变；深度相机、IMU、GNSS 和 PX4 仍运行。因此 `always_on=0`、`visualize=0`、关闭 world 阴影/光照、关闭 headless，以及单纯升级到 candB 都不能解决 RGB render target 泄漏。

因此 lifecycle 的 `lite` 默认档位以及 `standard`、`hd` 档位现都生成 `rgbd_camera` overlay；`lite` 是本轮唯一经过真实 SITL 内存实测的档位。高分辨率档位仍受 16g 容器上限与 `psim-memwatch` 90% 停止保护，需另行实测。

## 实验数据

原始 CSV 均在 `/data/scratch/polaris-sim/leak/`：

| 实验 | 配置 | 观测 | 结果 |
| --- | --- | --- | --- |
| baseline-lite | `gz-rendering-830-r1`、ogre2、headless、lite | 324 s，5.905→8.602 GiB | 8.94 MB/s |
| always-off | RGB `always_on=0`、`visualize=0` | 156 s，5.630→6.930 GiB | 8.95 MB/s；无改善 |
| candB | `gz-harmonic-latest-candB`（Gazebo 8.15） | 161 s，5.625→6.977 GiB | 9.02 MB/s；无改善 |
| shadows-off | world `<shadows>`、所有 `<cast_shadows>`、`<lighting>` 关闭 | 稳定段约 126 s | 约 8.95 MB/s；启动前 3 个样本为渲染初始化瞬态 |
| headless-off | `GZ_SIM_HEADLESS_RENDERING=0`、`HEADLESS=0` | 157 s，5.598→6.938 GiB | 9.16 MB/s；无改善 |
| rgb-off | 移除 `mono_cam` RGB sensor，保留 depth/IMU/GNSS | 159 s，5.107→5.107 GiB | 0 MB/s；平台 |
| HD | r1、ogre2、headless、1280×960@30 | 68 s，5.957→12.86 GiB | 109.0 MB/s（后段 113.1） |
| long-lite | r1、ogre2、headless、lite，连续观察 | 667 s，5.674→11.35 GiB；GPU2 峰值 11.7 GiB | 9.14 MB/s；前/后半程 9.09/9.18 MB/s，未见 cache 饱和 |
| leak2-rgbd-lite exploratory | r1、ogre2、headless、lite、`rgbd_camera`，Gateway 视频订阅开启 | 173 s 总窗；首个启动样本 4.9 GiB，之后 5.545–5.550 GiB | 未观察到持续增长；采样中有一次 50 s 间隔 |
| leak2-rgbd-default-10s | 默认 lifecycle lite overlay、`rgbd_camera`，Gateway 视频订阅开启 | 110 s，12 样本；初始化后 5.504–5.509 GiB，Gazebo 显存 2,870 MiB | 10 s 采样；RTSP H.264 640×480、yuv420p，抓帧色度非中性；无持续增长迹象 |
| leak2-rgbd-default-extended | 默认 lifecycle lite overlay，复核运行 | 约 181 s；初始化后 5.406–5.422 GiB，Gazebo 显存 2,886 MiB | 内存平台复现；采样命令耗时令间隔约 12 s，该次窗口比 180 s 上限多约 1 s，不作为定时复测 |
| ogre | `GZ_SIM_RENDER_ENGINE=ogre` | 启动失败 | OGRE 1.x GLX backend 报 `Couldn't open X display`，不适用于当前无 X 容器 |
| rc_cessna | lifecycle 真实启动尝试 | 未完成 | `rw_farm_iowa` 伪造 world 被 map/origin 校验拒绝；匹配的固定翼 world/scenario 需另行准备。未把失败冒充稳定性证据 |

## 可重复命令

所有真实启动都使用 `scripts/polaris_sim/up.sh`、`--gpu 2`、`--vehicles 1`、slot 5 的 `SitlLock`、`--memory=16g`，实验期间运行 `docker stats --no-stream`；实验结束使用对应 state 文件运行 `scripts/polaris_sim/down.sh`。候选镜像均使用本地 `--pull=never`。

例如 baseline：

```bash
POLARIS_SIM_STATE_FILE=/data/scratch/polaris-sim/leak/baseline-state.json \
  POLARIS_SIM_CONTAINER_MEMORY=16g \
  bash scripts/polaris_sim/up.sh --gpu 2 --vehicles 1 --camera-profile lite --no-video
```

RGB-off 和渲染开关由 lifecycle 诊断环境变量传入：`POLARIS_SIM_CAMERA_SENSOR`、`POLARIS_SIM_CAMERA_ALWAYS_ON`、`POLARIS_SIM_CAMERA_VISUALIZE`、`GZ_SIM_RENDER_ENGINE`、`GZ_SIM_HEADLESS_RENDERING`、`HEADLESS`。默认未设置这些变量，因此正常生命周期行为不变。`up.sh --image <本地 GPU tag>` 现在会把显式镜像传递到 supervise，便于离线候选镜像复测。

## 后续建议

RGBD 初始化会逐步占用约 5.5 GiB 容器 RAM；最终定时复测在后 70 秒内持平，较早的复核也观察到约 145 秒平台。两者均短于既有 667 秒原 RGB camera 长测，因此结论限于本次窗口，不能证明无限期零增长。GPU2 同时有其它训练负载；最终定时复测通过 `nvidia-smi` 进程表读到 Gazebo 峰值约 2.87 GiB，低于 4 GiB 限制，整卡峰值约 10.7 GiB。仍保留 16 GiB RAM watchdog。若只需要飞控/遥测回归或无图传采集，可用 `POLARIS_SIM_CAMERA_SENSOR=0`，但该模式不会发布 RTSP RGB 帧。
