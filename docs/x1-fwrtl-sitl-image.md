# X1 固定翼原生返航候选：ASSUMED_SIM

仅供 X1 的 Iowa SITL；不注册到车辆运行时，不改变 Altair 决策权、执行器或 PX4 保底。归档 `multidetect/px4-sitl-gazebo:gz-rendering-830-r1-gimbal-horizon4`、其参数和默认证据保持不变。

## 离线构建

从应用本补丁后的仓库根目录运行。先独立核验本地 `px4up-build:latest` 的完整镜像 ID，并以 `TOOLCHAIN_ID` 提供，不能把短 ID 或浮动 tag 当成输入。脚本固定 PX4 提交 `53afcc82b2a51a90424ff7e2c35583e491587eb5`、递归 gitlink、本地 Micro-CDR v2.0.1 提交及 archive SHA-256、云台移植补丁、4050/4051 脚本 SHA-256 和归档 rootfs 起点。可设 `PX4_SOURCE_ROOT`、`PX4_MICROCDR_ROOT`，不下载。`PX4_BUILD_JOBS` 默认 4，上限 8。

```bash
export PX4_VERIFY_PYTHON=/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python
systemd-run --scope -p MemoryMax=8G -p MemorySwapMax=0 \
  nice -n 15 scripts/build_px4_fwrtl_image.sh "$PWD/.tmp/fwrtl-build-r1" "$TOOLCHAIN_ID"
```

旧 `px4-main-53afcc82-gimbalid-r1` 只在 `gz-rendering-830-r1` 上覆盖 PX4 install 树，源码只附加设备 ID 修复，不能证明有 horizon4 云台路径，也不能证明未替换归档 Gazebo 插件。新配方从 horizon4 继承 Gazebo 软件包、插件、模型、世界、启动包装和宿主挂载接口，只覆盖 PX4 二进制、模块链接和 ROMFS。移植保留原 horizon 补丁新增的行为，适配上游已有 attitude include 和 legacy earth-yaw；设备 ID 赋值已在移植补丁中，不重复应用。未叠加旧 lockstep backport，也不把其它上游行为视为与归档等价。

`image-check.json` 记录不可变 ID、父层及配置验证、固件 SHA-256、Gazebo 和插件/钩子摘要、静态嵌入 Git 版本、实际 launcher 启动补丁在 tmpfs 上的执行，以及 4051/4050 和编译参数元数据。静态检查通过才打候选 tag。`manifest.txt`、`submodules.txt`、`applied.diff` 和 `build.log` 记录本次实际输入与产物摘要；配方可复跑，不承诺 OCI 创建时间或调试路径不同的构建产生相同 ID。通用 `verify_gpu_runtime_image` 只验证默认 GPU 镜像契约，不可用于认可这个独立候选；此候选匹配 X1 的不可变 ID 和 `x1_resume.image_check` 云台能力检查。

`/opt/multidetect-rtf-server.config`、`/opt/multidetect-rtf-models` 和风速插件路径可以是宿主挂载，而非镜像文件。不得在候选镜像中伪造这些挂载。静态检查证明的是镜像内已有 hook、包装和插件未变；实际挂载和 ABI 行为仍须在授权 SITL 中验证。

## 注册和证据

镜像静态验证不等于启用 FW RTL。`NAME.json` 必须恰好符合 `FwRtlBounds`；未获证的数值不要填零、默认值或旧轨迹回放数字。`NAME.validation.json` 记录独立审阅和每个界限的原始证据；其 SHA-256 写入 `validation_sha256`。注册器要求 sidecar 的 `basis=ASSUMED_SIM`、`status=reviewed`、`scope`、`image_id`、`px4_commit` 与清单一致。`wind_step` 和 `target_found` 各审各的，不可共享结果。注册结果是 demo 已支持的 `--fw-rtl-host-manifest` 文件对，不是新的车辆配置。

```bash
PYTHONPATH=src:. /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  -m scripts.register_x1_fwrtl_sitl register --mode sim \
  --manifest "$REVIEWED_MANIFEST" --output-dir "$PWD/.tmp/fwrtl-sitl-registration"
```

启动候选专用作业时显式设 `MULTIDETECT_PX4_PROFILE=main`，由 launcher 挂入相同 4050/4051，并选择 `SENS_GNSS0_DELAY`；不要改变归档默认配置。只从 `scenario_fw_rtl_readback` 确认实际参数和 MAVLink 版本匹配。构建和注册脚本均不读回运行参数、不启动 PX4/Gazebo 仿真。要为缺失界限采样，应另行批准独立的 SITL 采集，不得用虚构界限启动有证明门槛的 demo。
