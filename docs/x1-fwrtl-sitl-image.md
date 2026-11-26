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

## Iowa FW Hold → RTL 独立测量

`scripts/measure_x1_fw_rtl.sh` 仅接受显式 `--mode sim`，启动独占 PX4 main profile 的 SITL。入口使用已校验的 Iowa 五步固定任务，通过现有 collector 飞至固定翼巡航。第一次满足 Case C 的 FW Hold 入口范围、速度和 Iowa 巡航约束时，收回普通路线的写权限，向 Case C 的 `hold:yard-orbit` 发送 `DO_ORBIT`（北 320 m、半径 80 m，85 m 单元的圆心为 402.404 m AMSL）。收到 ACK 并从同一 PX4 确认 Hold 模式和目标后，按 PX4 时钟等待指定偏移，检查飞行状态和轨道位置，再原样发送一次 `NAV_RETURN_TO_LAUNCH`（20，七个参数全零）。这个测量入口跳过 RTL MotionProof，正常 X1 runner 没有对应参数或入口。PX4 负责着陆和解除武装。

采集需要候选 `multidetect/px4-sitl-gazebo:fwrtl-53afcc82-r1` 的完整镜像 ID `sha256:6ef7805c139866d61a2f20c7c260a789179aff984522654069e273ab3ab8aa79`、匹配的 `static_verified` 回执，以及已获准的 5–7 号 slot。启动沿用现有飞行锁、GPU 租约、资源闸门及容器所有权。每个单元在新容器中运行。默认 48 个单元使用 Hold 确认后 30/39/48/57 s、0/4/6 m/s 风速、600/1800 s 电池 1 放电常数和 85/95 m AGL。风向为从北顺时针量起的**吹向** 333.435°。有风单元从 Hold 确认开始用现有 Gazebo 插件按仿真时间在 4 s 内渐增，解除武装后恢复 SDF 基线。记录实际 FW 航向、地面航迹方向、起始 SOC 与风估计；参数配置本身只代表试验输入。

从已应用补丁的仓库根目录执行。由运行者设置 `SLOT` 为已获准的空闲 slot。保留运行目录内的 `matrix.json`、每个单元的 `probe.json`、collector 的 MAVLink JSONL、原始 ULog 和 `container-evidence/index.json`。

```bash
ref=/data/scratch/cx-wc-sampling
src=/data/scratch/x1-scn-run
probe_root=$(mktemp -d "$PWD/.tmp/x1-fw-rtl.XXXXXXXX")
mkdir "$probe_root/world"
cp "$ref/.tmp/x1-iowa-world/rw_farm_iowa_w6km__clear_noon.sdf" "$probe_root/world/"
export TMPDIR="$probe_root" MULTIDETECT_SITL_SLOTS=8
export MULTIDETECT_RTF_WORLD_ROOT="$probe_root/world"
export MULTIDETECT_SIM_FUEL_RESOLVED_ROOT=/data/datasets/multidetect-sim-assets/wide-collect/assets/rw_farm_iowa_w6km/restored/models
unset MULTIDETECT_RTF_MODEL_ROOT
probe=(--mode sim --slot "${SLOT:?set an admitted slot 5, 6 or 7}"
  --image-check "$ref/.tmp/fwrtl-build-r3/image-check.json"
  --mission-package "$src/iowa-context.json" --recorded-plan "$src/recorded-plan.json"
  --wind-plugin "$src/wind-plugin-build/libx1_wind_airspeed.so"
  --offsets-s 30 39 48 57 --winds-mps 0 4 6 --drains-s 600 1800
  --altitudes-agl-m 85 95 --repeats 1 --wind-toward-deg 333.435)
scripts/measure_x1_fw_rtl.sh "${probe[@]}" --output "$probe_root/prepare" --prepare-only
# 以下命令启动 SITL，由运行者执行。先飞一个 pilot，再续跑同一矩阵。
scripts/measure_x1_fw_rtl.sh "${probe[@]}" --output "$probe_root/matrix" --cell o30-w0-b600-a85-r1
scripts/measure_x1_fw_rtl.sh "${probe[@]}" --output "$probe_root/matrix" --resume
PYTHONPATH=src:. PYTHONUTF8=1 /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  -m scripts.analyze_x1_fw_rtl_probe --mode sim --matrix "$probe_root/matrix" \
  --output "$probe_root/analysis" --name iowa-fw-rtl
```

`--prepare-only` 只编译任务并写矩阵输入。需要重试中断单元时，先把原单元目录移动到同一运行目录的另外一个证据位置，再带同一组输入使用 `--resume`。可加 `--cell` 单独重试。每次分析选一个新的输出目录。

候选启动器在 `/opt/px4-gazebo` 下显式传入 `-s etc/init.d-posix/rcS`，该目录就是 PX4 rootfs；因此 console 中的 `./fs/log/...ulg` 实际位于 `/opt/px4-gazebo/fs/log/`。探针在武装前确认该目录中只有一份非空 ULog，在确认解除武装并停止 logger 后、容器移除前将同一文件直接复制到 `container-evidence/px4-ulog`。现有 collector 的复制逻辑不变，仍会尝试旧路径；以 `index.json` 中的 `ulog_files` 判断是否成功归档。

分析以原始 ULog 的 Navigator 和 VTOL 状态划分七段，交叉核验 MAVLink 命令与时钟、Home、RTL 参数以及触地和解除武装。sidecar 记录分段耗时、相对证明扫掠盒的水平和垂直误差、后转损失、距 Home 的触地点偏差、滚转角、空速、地速、升降速度、加速度、角速度及 SOC 推算的耗能。每个拟议上界记录逐运行值、极值运行 ID、中位数和公式。对每项取观测最大值，加 `max(观测最大值 × 20%, 该量绝对余量)`，再应用 proof 的必要下限并向上取三位小数。如果最大值超过 `max(2 × 中位数, 中位数 + 4 × 绝对余量)`，拒绝整个清单。要求所有声明单元齐全且至少 16 个有效运行、85 m 无风样本覆盖四个航向象限、相邻放电档的实测入口 SOC 中位数至少差三个百分点，以及风、遥测、航迹和机型限制检查全部通过。

离线诊断可用 `--allow-partial` 跳过没有单元目录的未飞单元；这些单元仍计入声明矩阵，因此结果继续拒绝注册，直到完整矩阵满足规则。默认严格模式会逐项报告 `probe_missing`。已完成但没有归档 ULog 的单元会明确报告 `ulog_missing`。

退出 `2` 并生成 `NAME.validation.json` 时，查看 `refusals` 与逐单元测量结果。两项功率界限要求有实测电压和电流。目前候选提交的 `BatterySimulator.cpp` 发布 `current_a=-1`，因此预期只能得到诊断 sidecar。`SOC × 72 Wh` 给出合成电池的耗能估计。满足所有条件时才会生成 `NAME.json`，其 scope 哈希覆盖采样网格和资产，`status=reviewed` 表明通过该网格规则。Case C 的 wind_step 使用 `8 N / -4 E m/s` 及不同事件时序，需要独立的范围证据和审阅。
