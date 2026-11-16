# Gazebo RGB 相机内存增长实验

日期：2026-11-17（工作站本地时钟，实验文件名使用 UTC）

## 结论

`x500_mono_cam` 的增长来自 Gazebo RGB camera render target。真实滨海湾 world、GPU2、16 GiB 容器上限、无视频订阅者时，`lite`（640×480@10）稳定约 8.9 MB/s，HD（1280×960@30）约 109–113 MB/s。容器内最大进程始终是 `gz sim`，不是 PX4、Gateway 或视频编码器。

在同一个 x500 模型中移除 `mono_cam` 的 RGB `<sensor type="camera">` 后，RSS 在约 159 秒内保持 5.107 GiB 不变；深度相机、IMU、GNSS 和 PX4 仍运行。因此 `always_on=0`、`visualize=0`、关闭 world 阴影/光照、关闭 headless，以及单纯升级到 candB 都不能解决 RGB render target 泄漏。

当前没有找到既保留实时 RGB 图传又能根治该上游泄漏的 Gazebo 参数。生命周期默认继续使用 `lite`、`ogre2`、headless 和 16g 上限；已有 `psim-memwatch` 会在 90% 上限停止所属容器，避免工作站被 OOM 拖垮。`POLARIS_SIM_CAMERA_SENSOR=0` 仅用于诊断/无图传模式，不应作为 Polaris 图传默认值。

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
| ogre | `GZ_SIM_RENDER_ENGINE=ogre` | 启动失败 | OGRE 1.x GLX backend 报 `Couldn't open X display`，不适用于当前无 X 容器 |
| rc_cessna | lifecycle 真实启动尝试 | 未完成 | `rw_farm_iowa` 伪造 world 被 map/origin 校验拒绝；匹配的固定翼 world/scenario 需另行准备。未把失败冒充稳定性证据 |

## 可重复命令

所有真实启动都使用 `scripts/polaris_sim/up.sh`、`--gpu 2`、`--vehicles 1`、slot 5 的 `SitlLock`、`--memory=16g`，每 10 秒运行 `docker stats --no-stream`；实验结束使用对应 state 文件运行 `scripts/polaris_sim/down.sh`。候选镜像均使用本地 `--pull=never`。

例如 baseline：

```bash
POLARIS_SIM_STATE_FILE=/data/scratch/polaris-sim/leak/baseline-state.json \
  POLARIS_SIM_CONTAINER_MEMORY=16g \
  bash scripts/polaris_sim/up.sh --gpu 2 --vehicles 1 --camera-profile lite --no-video
```

RGB-off 和渲染开关由 lifecycle 诊断环境变量传入：`POLARIS_SIM_CAMERA_SENSOR`、`POLARIS_SIM_CAMERA_ALWAYS_ON`、`POLARIS_SIM_CAMERA_VISUALIZE`、`GZ_SIM_RENDER_ENGINE`、`GZ_SIM_HEADLESS_RENDERING`、`HEADLESS`。默认未设置这些变量，因此正常生命周期行为不变。`up.sh --image <本地 GPU tag>` 现在会把显式镜像传递到 supervise，便于离线候选镜像复测。

## 后续建议

要保留 RGB 图传，优先把 16 GiB watchdog 视为临时保护，并在上游 gz-rendering 修复或可替换渲染后端可用时重新验证。若只需要飞控/遥测回归或无图传采集，可用 `POLARIS_SIM_CAMERA_SENSOR=0`，该模式在本次实验中消除了增长，但不会发布 RTSP RGB 帧。
