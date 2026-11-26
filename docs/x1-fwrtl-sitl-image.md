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

镜像静态验证不等于启用 FW RTL。`NAME.json` 必须恰好符合 `FwRtlBounds`；未获证的数值不要填零、默认值或旧轨迹回放数字。`NAME.validation.json` 记录独立审阅和每个界限的原始证据；其 SHA-256 写入 `validation_sha256`。清单参数需包含武装前写入并核验的 `RTL_DESCEND_ALT=73` 和 `FW_AIRSPD_MIN/TRIM/MAX=12/15/18 m/s`。审阅 sidecar 的 `fw_airspeed_parameters` 必须等于清单三项读回；还要核对 `rule.airspeed`、逐格实测值与拒收结果。注册器要求 sidecar 的 `basis=ASSUMED_SIM`、`status=reviewed`、`scope`、`image_id`、`px4_commit` 与清单一致。`wind_step` 和 `target_found` 各审各的，不可共享结果。注册结果是 demo 已支持的 `--fw-rtl-host-manifest` 文件对，不是新的车辆配置。

```bash
PYTHONPATH=src:. /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  -m scripts.register_x1_fwrtl_sitl register --mode sim \
  --manifest "$REVIEWED_MANIFEST" --output-dir "$PWD/.tmp/fwrtl-sitl-registration"
```

启动候选专用作业时显式设 `MULTIDETECT_PX4_PROFILE=main`，由 launcher 挂入相同 4050/4051，并选择 `SENS_GNSS0_DELAY`；不要改变归档默认配置。只从 `scenario_fw_rtl_readback` 确认实际参数和 MAVLink 版本匹配。构建和注册脚本均不读回运行参数、不启动 PX4/Gazebo 仿真。要为缺失界限采样，应另行批准独立的 SITL 采集，不得用虚构界限启动有证明门槛的 demo。

## Iowa FW Hold → RTL 独立测量

### 武装前固定参数

在 PX4 `53afcc82b2a51a90424ff7e2c35583e491587eb5`，固定翼这三项是设定值边界与回退值的最小固定集合，不是物理限速器：

| 参数 | 值 | 对返航的作用与源码 |
| --- | ---: | --- |
| `FW_AIRSPD_MAX` | 18 m/s | profile 上限；`src/lib/fw_performance_model/PerformanceModel.cpp` 提供最大校准空速；`FwLateralLongitudinalControl.cpp::adapt_airspeed_setpoint` 用于限幅。 |
| `FW_AIRSPD_TRIM` | 15 m/s | 返航无显式速度时的默认设定值。`src/modules/navigator/rtl_direct.cpp::on_activation` 重置巡航速度；控制器在未指定有效校准空速时取 trim。 |
| `FW_AIRSPD_MIN` | 12 m/s | 保留 4051 的低速基线。`PerformanceModel::getMinimumCalibratedAirspeed` 按载荷/襟翼修正；不可为了通过实测闸门而降低防失速余量。 |

`src/modules/navigator/navigator_main.cpp` 的 FW `DO_ORBIT` 处理器将 `param1` 用作圆半径/方向，进入盘旋时重置速度设定，不用 `param2` 限速。`NAV_LOITER_RAD`、`RTL_LOITER_RAD` 是盘旋几何，不固定速度。控制器按 EAS/TAS 换算，载荷修正的最低速度必要时优先于校准上限；风、跟踪瞬态和物理响应也不会被参数钉值截断。归档 `o39-w6-b1800-a85-r1` 的 ULog 活动 `P` 项已是 12/15/18 且 `FW_WIND_ARSP_SC=0`（`Q` 的 10/20 是编译默认，不是活动值），但同批 30 个可读量测格中 10 格超过 18，最高 22.99824 m/s。新钉值只固定配置和准入，**不证明这套镜像实际能保持 ≤18 m/s**。不得改成 23 m/s，也不得夹紧遥测或调低防失速下限来帮助通过。

所有格武装前写入 `RTL_DESCEND_ALT=73 m`；`RTL_RETURN_ALT` 按格写入。分析核验武装前、解除武装后和 ULog 的参数与武装边界。返航高度为 `max(激活高度, Home + RTL_RETURN_ALT)`，盘旋目标为 `min(返航高度, Home + RTL_DESCEND_ALT)`。73 m 相对 Home，比 40 m AGL 下限加 12 m DEM 误差高约 21 m；仍须核查盘旋地形、跟踪和转场失高。共享 MC 返航几何、时间和功耗不变；探针专用 100 m 不是共享 MC 设定。

### 38 格采样与覆盖

`scripts/measure_x1_fw_rtl.sh` 仅接受显式 `--mode sim`，使用受守卫的 Iowa 五步固定任务。除备用盘旋族以外，探针确认 FW Hold 入口后独占写权限，向 `hold:yard-orbit` 发 `DO_ORBIT`（北 320 m、半径 80 m，85 m 格圆心为 402.404 m AMSL）；同一 PX4 确认 Hold 后按 PX4 时钟等待指定偏移。然后原样发一次 `NAV_RETURN_TO_LAUNCH`（20，七参数全零）。PX4 负责返航、着陆及解除武装。这是独立 `ASSUMED_SIM` 测量，不是产品 runner 入口；Altair 仍负责产品飞行决策，执行器只接受或拒绝并按原样执行，PX4 是最后一道安全保障。

| `family` | 声明格数 | `return_alt`（Home 上方） | 输入及必要证据 | 预计每格 |
| --- | ---: | ---: | --- | ---: |
| `core` | 32 | 60 m | Hold +30/39/48/57 s × 0/6 m/s × 600/1800 s 放电常数 × 75/85 m AGL；每组合 1 次 | 6 分钟 |
| `climb` | 2 | 100 m | 85 m AGL、静风、1800 s、Hold +39 s；必须看到 Navigator `CLIMBING` 沿已建立 Hold 的圆心/半径盘旋 | 7 分钟 |
| `alternate-loiter` | 2 | 100 m | 85 m AGL、静风、1800 s、从守卫通过的 FW OFFBOARD 巡航直接 RTL，不发 `DO_ORBIT`；必须看到 `CLIMBING` 在激活点按 `NAV_LOITER_RAD` 盘旋 | 6 分钟 |
| `case-c` | 2 | 60 m | 85 m AGL、1800 s；route +21.0…30.5 s 原样 `wind_step`，4 s 升/降坡，`+8 N / −4 E m/s`；route +25.440…<30.500 s 发 Hold，恢复基线后于 Hold 确认 +167 s 原样 RTL | 9 分钟 |

默认总计 **38 格、236 分钟（3 小时 56 分钟）**，不含退避、排队及重飞。归档 32 格用时中位数约 303 s；新族用时为排程估计，尚未飞行验证。`case-c` ID 中的 `w0` 指未启用 core 恒风，实际风由归档 schedule 控制。这个复刻量测 Case C 的风/Hold/RTL 运动过程，不声称复现曾被拒绝的 Altair 返回准入、`target_found` 或建立产品注册范围。Core 有风格从 Hold 确认时起在 4 s 内渐增，风**吹向**从北顺时针 333.435°。使用候选镜像完整 ID `sha256:6ef7805c139866d61a2f20c7c260a789179aff984522654069e273ab3ab8aa79` 和匹配的 `static_verified` 回执；只用已获准的 5–7 号 slot，复用现有资源闸门和容器所有权。

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
  --case-c-schedule "$src/faults-fw-wind.json"
  --offsets-s 30 39 48 57 --winds-mps 0 6 --drains-s 600 1800
  --altitudes-agl-m 75 85 --repeats 1 --wind-toward-deg 333.435)
scripts/measure_x1_fw_rtl.sh "${probe[@]}" --output "$probe_root/prepare" --prepare-only
# 以下命令启动 SITL，由运行者执行。先飞一个 pilot，再续跑同一矩阵。
scripts/measure_x1_fw_rtl.sh "${probe[@]}" --output "$probe_root/matrix" --cell o30-w0-b600-a85-r1 --max-attempts 3
scripts/measure_x1_fw_rtl.sh "${probe[@]}" --output "$probe_root/matrix" --resume --max-attempts 3
PYTHONPATH=src:. PYTHONUTF8=1 /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  -m scripts.analyze_x1_fw_rtl_probe --mode sim --matrix "$probe_root/matrix" \
  --output "$probe_root/analysis" --name iowa-fw-rtl
```

`--prepare-only` 只编译任务并写输入。正式矩阵须使用一个新目录，不能从 prepare 目录续飞。`--resume` 比对完整 `matrix.json`、输入资产和采集脚本 SHA-256，跳过证据完整的格；`--cell` 可单飞或结合 `--resume` 单独补飞。`--max-attempts 2–12` 开启内建重试（默认 1 次，不重试）；阻断准入、未完成采集、缺失/坏 ULog 或 dropout 时，把原始尝试存入 `failed-evidence/<cell>.tryNN/`，附 `probe-reflight.json`，等待 `--retry-delay-s`（默认 120）后重试。失败编号和预算跨 resume 保留；耗尽时报失败，不能当作完整矩阵。分析报告的 `reflight_cells` 列出 dropout，可用相同输入带 `--resume --max-attempts 3 --cell <ID>` 自动留档并重飞（预算耗尽时须显式增加限额）。每次分析用新的输出目录。

候选启动器在 `/opt/px4-gazebo` 下显式传入 `-s etc/init.d-posix/rcS`，该目录就是 PX4 rootfs；因此 console 中的 `./fs/log/...ulg` 实际位于 `/opt/px4-gazebo/fs/log/`。探针在武装前确认该目录中只有一份非空 ULog，在确认解除武装并停止 logger 后、容器移除前将同一文件直接复制到 `container-evidence/px4-ulog`。现有 collector 的复制逻辑不变，仍会尝试旧路径；以 `index.json` 中的 `ulog_files` 判断是否成功归档。

分析以原始 ULog 的 Navigator 和 VTOL 状态划分七段，交叉核验 MAVLink 命令与时钟、Home、RTL 参数以及触地和解除武装。sidecar 记录分段耗时、相对证明扫掠盒的水平和垂直误差、后转损失、距 Home 的触地点偏差、滚转角、空速、地速、升降速度、加速度、角速度及 SOC 推算的耗能。每个 `MEASURED_SIM` 上界记录逐运行值、极值运行 ID、中位数和公式。对这些量取观测最大值，加 `max(观测最大值 × 20%, 该量绝对余量)`，再应用 proof 的必要下限并向上取三位小数。如果最大值超过 `max(2 × 中位数, 中位数 + 4 × 绝对余量)`，拒绝整个清单。要求所有声明单元齐全且至少 16 个有效运行、85 m 无风样本覆盖四个航向象限、相邻放电档的实测入口 SOC 中位数至少差三个百分点，以及风、遥测、航迹和机型限制检查全部通过。

分析逐样本核查从 RTL 派发采样到实际开始 FW→MC 转换（含派发段）的 `airspeed_validated.true_airspeed_m_s`，不使用均值、校准空速或地速替代。PX4 `Tools/simulation/gz/models/airspeed/model.sdf` 未配置噪声；ULog 字段是 binary32，18 附近的一档间距 `2⁻¹⁹ ≈ 0.00000190735 m/s` 为保守量化容差，不把历史超速当传感器噪声。任何样本超过 `18 + 2⁻¹⁹ m/s` 就拒收该运行和清单。`matrix.v2` 必须重飞旧 32 个组合及六个新格；旧矩阵/日志不能混入。除声明的 `RTL_RETURN_ALT` 与电池档外，参数一致性闸门不变。三族覆盖证据必须齐全，但不参与 core bounds 拟合，也不授权备用盘旋或 Case C 的运行时 scope。

离线诊断可用 `--allow-partial` 跳过没有单元目录的未飞单元；这些单元仍计入声明矩阵，因此结果继续拒绝注册，直到完整矩阵满足规则。默认严格模式会逐项报告 `probe_missing`。已完成但没有归档 ULog 的单元会明确报告 `ulog_missing`。

退出 `2` 并生成 `NAME.validation.json` 时，查看 `refusals` 与逐单元测量结果。每一项功率上界若所有单元均无法得到有效的 `V × A` 峰值，沿用 Iowa 后缀模型并标记 `ASSUMED_SIM`。`fw_peak_power_w = 2 × loiter_power_w = 344 W`，`hover_peak_power_w = max(climb_power_w, hover_power_w, descent_power_w) = 750 W`，用于后转、MC 和着陆。`derivations` 保留机型文件 SHA-256、公式、输入功率及各单元的空测量值。若已有部分单元的有效峰值，则要求该项在所有单元均有峰值，并照常进行异常值检查和加余量。只有这两项可以使用假设功率。当前候选的 `BatterySimulator.cpp` 发布 `current_a=-1`，`SOC × 72 Wh` 只用于估计合成电池的耗能，无法给出瞬时功率。满足所有条件时才会生成 `NAME.json`，其 scope 哈希覆盖采样网格和资产，`status=reviewed` 表明通过该网格规则。`python -m scripts.study_x1_fw_envelope "$probe_root/study" --matrix "$probe_root/matrix" --sidecar "$probe_root/analysis/iowa-fw-rtl.validation.json"` 分族输出诊断 JSON/CSV，并单列 Case C 实测与归档反事实；不签发或注册 bounds。
