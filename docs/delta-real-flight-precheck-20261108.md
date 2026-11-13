# 三角翼真机前检查清单（2026-11-08）

状态：**尚未完成真机前放行**。本文记录现有仿真镜像和代码事实，供逐项取证；没有修改飞行参数，没有运行 HIL 或真机。静态/单元门禁不代表飞行门禁通过。

## 版本与证据

- 集成提交：`3b817dd1`，将 `env/workstation-paths` 合入 `wt/cx-delta-fw`，自动合并，无冲突。
- 实际镜像 ID：`sha256:445d17a7ae7e34dbddc826b68c08c705d4892a3b3997e3f3a0bb44072a5ef8f2`（本地 `multidetect/px4-sitl-gazebo:gpu`）。
- 无网络、无 GPU 的一次性容器执行 `px4-ver all`：PX4 `83c4f4e580558816f0e398918f66358988004696`，版本 `1.18.0`，构建时间 `2026-08-09 04:08:10`。原文在工作树 `.tmp/px4-version.txt`。
- 以下“实读”来自本轮自行启动的固定相机实例 `md-smoke-delta-slot-3-episode-0000-814f4133`，只执行 `px4-param show`；完整原文在 `.tmp/delta-runtime-failsafe-parameters-20261108.txt`。镜像元数据为 `/opt/px4-gazebo/etc/extras/parameters.json.xz`。
- 参数必须在真机启动后重新导出。`set-default` 不能保证覆盖已有持久化参数，镜像元数据的 default 也不等于运行值；例如 `COM_LOW_BAT_ACT` 元数据为 0，而本轮实例实读为 3。

## 姿态失控与终止

| 检查项 | 本轮仿真实读 | 真机前要求及待验证证据 |
|---|---|---|
| `FD_FAIL_P` | 60°，未被本地 airframe 覆盖 | 导出真机实值；记录 MC/FW 两套坐标下的边界测试与故障事件 |
| `FD_FAIL_R` | 70°，airframe 显式默认 | 记录受控越界是否触发，确认允许机动范围与阈值不冲突 |
| `FD_FAIL_P_TTRI` / `FD_FAIL_R_TTRI` | 均 0.3 s | 检查短暂扰动、持续越界、恢复时序；不靠提高阈值掩盖失控 |
| `CBRK_FLIGHTTERM` | 121212 | 该值禁止 FailureDetector/FMU 丢失触发的常规 flight termination。必须明确真机策略、输出 failsafe 值、降落伞/外部 ATS 和电机行为，并在拆桨条件验证；不能将“检测到故障”等同“已终止” |
| `COM_LKDOWN_TKO` | 当前镜像未找到 | 不复制旧版参数名；应按本固件 Commander/失败处理代码与事件验证起飞保护 |
| `FD_ACT_EN` / `FD_IMB_PROP_THR` | 镜像元数据为 0 / 30，未做真机实读 | 检查实际 ESC 电流遥测与振动数据是否足以支持检测；无遥测时不宣称电机故障已被覆盖 |
| `FD_EXT_ATS_EN` | 镜像元数据为 0，未做真机实读 | 若使用外部 ATS，核对接线、PWM阈值、供电与实际动作；本项目未验证 |

对应固件的 [FailureDetector.cpp](https://github.com/PX4/PX4-Autopilot/blob/83c4f4e580558816f0e398918f66358988004696/src/modules/commander/failure_detector/FailureDetector.cpp#L92) 对尾座机单独处理：转换阶段令姿态检查输入为零，FW 阶段先将姿态旋转到平飞坐标再比较。因此不能直接用 MC 机体 pitch 判断 FW 是否越过 60°，也不能声称转换阶段由同一姿态门槛保护。后续应保存 `is_vtol_tailsitter`、`in_transition_mode`、`vehicle_type`、姿态和 `failure_detector_status` 的同时间轴证据。

## 转换、返航与失联

| 检查项 | 本轮仿真实读 | 真机前要求及待验证证据 |
|---|---|---|
| `VT_TRANS_TIMEOUT` | 20 s（本地 airframe） | 故意未能完成前转换时，验证中止转换、MC 回退和后续动作 |
| `VT_FW_MIN_ALT` / `VT_FW_QC_P` / `VT_FW_QC_R` | 全为 0 | 当前没有启用这些高度/姿态 quad-chute 阈值；由飞行范围、回转高度和回退能力确定策略后做故障注入 |
| `VT_FW_QC_HMAX` / `COM_QC_ACT` | 0 / 0 | 分别核对实际生效条件与回退后 Return 动作；不能仅凭名字认为高度/姿态保护已启用 |
| `FW_THR_MIN` | airframe 默认 0.05 | 回转换需验证四电机不同时停转、下坠幅度、回 MC 后爬升；此前确有零推力回转换风险 |
| `COM_OF_LOSS_T` / `COM_OBL_RC_ACT` | 1 s / 0（Position） | 断开机载计算机而保持/失去 RC 的两种情况分别测；确认 Position 的定位前提以及定位丢失后的升级路径 |
| `NAV_DLL_ACT` | 0（Disabled） | GCS 心跳消失默认不触发动作；真机应明确允许失联时间及选定动作并验证 |
| `NAV_RCL_ACT` | 2（Return） | 单测 RC 丢失、GCS 丢失、同时丢失；检查 `COM_RC_LOSS_T`、`COM_DL_LOSS_T` 和 exception 位图，不照搬枚举数字到其他固件 |
| `RTL_TYPE`、返航高度、降落点 | 尚未真机验证 | 核对 home、起飞方向、FW/MC返航路径、围栏和剩余电量，完整验证 RTL→回转换→落地→disarm |

[PX4 官方安全配置](https://docs.px4.io/main/en/config/safety) 可用于选择待测场景；执行时以本机确切固件参数与实际事件为准。

## 空速链路与启动告警

- [ ] 真机导出并核对 `SYS_HAS_NUM_ASPD`、`ASPD_PRIMARY`、`ASPD_DO_CHECKS`、`ASPD_FALLBACK`、`FW_USE_AIRSPD`；这里没有取得真机实读值，不将源码默认值当成真机配置。检查选中的来源是否与实装皮托管/差压传感器一致，以及失效后实际切换来源和动作。
- [ ] 保存 `airspeed_validated` 的 timestamp、`airspeed_source`、校准/真空速及各传感器有效性，与 `failure_detector_status`、转换状态、姿态和控制输出对齐。覆盖无数据、冻结读数、创新/负载检查和恢复；仅有正的 `VFR_HUD.airspeed` 或解锁成功不能证明该链路有效。
- [ ] 记录并解释启动告警的清除过程。2026-11-09 候选镜像复验的固定相机 episode `091e36db` 在启动阶段出现 `Preflight Fail: Airspeed selector module down`，之后已解锁起飞并进入 FW sustain，后续 `VFR_HUD.airspeed` 约 15.48 m/s；这不足以判定有持续故障，也不足以证明全部空速保护已生效。原始证据保存在仓库外 `runs/delta-candidate-recheck-20261109/output-r2/fixed/episodes/episode-0000-091e36db/mavlink.jsonl`，最终门禁另行判定。

确切 PX4 `83c4f4e580558816f0e398918f66358988004696` 的 [airspeedCheck.cpp](https://github.com/PX4/PX4-Autopilot/blob/83c4f4e580558816f0e398918f66358988004696/src/modules/commander/HealthAndArmingChecks/checks/airspeedCheck.cpp) 在无可用或超过 2 s 的 `airspeed_validated` 数据时进入该告警分支；检查还受 `SYS_HAS_NUM_ASPD` 和机型状态约束。该告警不能仅凭字符串推断模块永久停止。同版本参数定义中的 `ASPD_DO_CHECKS` 只在 `ASPD_PRIMARY>0` 时应用；应核对实际来源和有效位，而非只看参数已设置。

## 电源、导航、执行器及共用软件

- [ ] 真机导出 `COM_LOW_BAT_ACT`、`BAT_*_THR`、`COM_FLTT_LOW_ACT`、`COM_FLT_TIME_MAX`，校准电池电压/电流；验证低电量、返航能量不足和掉电动作。本轮仿真 `COM_LOW_BAT_ACT=3`，其余未实测。
- [ ] 核对 `GF_ACTION`、围栏边界、`GF_MAX_HOR_DIST` / `GF_MAX_VER_DIST` 及定位丢失动作；零距离默认值不代表已有有效边界。
- [ ] 检查 `CBRK_IO_SAFETY`、`CBRK_SUPPLY_CHK`、传感器校准、arm checks、机体方向、电机顺序/转向、failsafe 输出和停桨开关；拆桨验证后记录签核。
- [ ] 仿真专用 `SENS_EN_MAGSIM=1`、`SENS_GPS0_DELAY=0`、`EKF2_ASP_DELAY=0` 仅描述无延迟仿真传感器，不能直接下发真机；真机保留实测延迟、磁罗盘和空速校准。
- [ ] 重量、重心、惯量、推力、结构与电机温升须有实体测量。现有模型参数来自估计，飞行仿真不能证明结构与推力余量。
- [ ] 固定相机和云台各自验证 RGB 时间戳、机体/云台外参、三轴指令与回读、桨速、图像遮挡。机载与仿真走同一任务策略入口。
- [ ] 现役身份 r21id-r3 + 规划 r24 + 规划前预检保持；新模型仅在完整门槛通过后替换。身份确认独立视点规则亦须在合并后的 21 任务双次基线上复核。
- [ ] 三角翼严格飞行门禁、合并后 21/21 基线和用户要求的双次回归均保存不可混用的输出目录。2026-11-08 固定相机/云台两版严格冒烟已通过，完整证据见 [集成飞行门禁记录](delta-integration-gate-20261108.md)；最终集成 head 的 21×2 基线仍待完成，不能据此放行 main。
- [ ] 发布前审查私有设计及凭据；原始 CAD/报告留在本地或获授权的私有位置，不新增公开传播。检查记录保存在仓库外 `runs/delta-publish-asset-audit-20261108.json`。

未完成项均保留未勾选；本文不批准实飞、不替代机型适航/操作签核。

<a id="scope-630918-20261109"></a>

## 2026-11-09 补充：版本范围与真机放行边界

以上正文保留为历史检查记录，其中“本轮仿真实读”专指 2026-11-08 的旧镜像实例，不表示下列候选或任何真机已重新导出参数。本补充没有执行参数读写、故障注入、HIL 或真机操作。

### 版本必须按组件和实例区分

| 证据 | 代码 / 镜像范围 | 能证明的内容 |
|---|---|---|
| 原 `px4-ver all` 和 `px4-param show` | 集成 `3b817dd1`；旧 `:gpu` image `445d17a7…`；固定实例 `814f4133` | 原日志报告 `PX4_SITL`、git `83c4f4e580558816f0e398918f66358988004696`、`PX4 version: 1.18.0 80`、2026-08-09 构建。该版本字符串不应扩大成全部“PX4 1.18”或真机固件的通用结论 |
| PX4 lifetime 候选双变体 | 源 `21dcab8b…`；image `b5c576ce…`；`091e36db` / `8e19af28` | 保留上节独立仿真结果；PX4 源锚仍为83c，但已应用 lifetime-r1 补丁，不能把它当成未修改的旧二进制 |
| transport 候选双变体 | 源 `6f05947031df137904010fba8f691824c59b6811`；image `630918d33596…`；`fa0527b2` / `1d83eda7` | 本节下述严格SIM复验，仅此源/镜像/两变体范围；不能直接改写为最终交付8c或真机已经全部验收 |

完整候选身份及组件链：

- 镜像：`sha256:630918d335967a9ac130cde821080e7b45193bc1a876d10f9508d657ece2bcb0`，父镜像 `sha256:b5c576ce15b8a75935313bc3e8dc4cf9940778226b28e835064d7581c3fe72c6`。
- PX4：基于上述83c源码，lifetime-r1补丁 SHA256 `43bfe32d5d7572b866b3d411c2394d941b6bcc8e19ace5bdd6e230845cdf0559`；实际候选二进制 SHA256 `9c2280e005ad426f01cdffddf2114e1e4b13e26aa9f5996c4b52cdfa9e722d99`。
- gz-transport13：修补源 `09266eb0eb381d090b9fa7f784636ef6eb0335a3`；库 SHA256 `aff44fe0ab4e1452c78b65e97c76a3f99f9d132777e96840666186f3d8cc82fe`。这是固定镜像组件范围，不是发行版ABI等价或所有Gazebo版本已覆盖的证明。

项目父目录 `runs/px4-lockstep-candidate-ready-20261108.json`、`runs/transport-candidate-ready-20261109.json` 保存组件身份；双变体原证据在 `runs/delta-transport-recheck-20261109/output/`，独立复算在 `runs/delta-transport-independent-audit-20261109/`。后者 `audit.json` 的 SHA256 为 `f0bf3a8665059fa883b1de289e18d9af0f12dd9feadeae26846ee4ffefe86621`，包含原始输入hash与逐项重算结果。

**参数实读仍有缺口。** `FD_FAIL_P=60°`、`FD_FAIL_R=70°`、两项TTRI=0.3s、`CBRK_FLIGHTTERM=121212`及正文其它实读值，仍只属于旧445镜像的814f4133实例。本附录未在630候选重新执行 `px4-param show`；两份630复验包也没有这些参数的重新导出证据。源码相同或补丁没有意图改参数，均不能代替当前启动实例/持久化参数的实值。真机还须单独保存确切固件hash、版本输出、完整参数导出与机型/启动实例身份，再验证失败事件、动作及输出，不能将表中旧值写成真机保护已通过。

### 630严格门禁通过不等于精准返航或自动停桨

原门禁复算保持 `target_altitude_m=5`、`position_error_m=8`，没有改阈值或实际评测。固定15项、云台16项required fields及各自8项三角翼严格字段均为true，两个collector均为0/complete、各一个原始episode且无retry。

| 原始证据 | 固定 `fa0527b2` | 云台 `1d83eda7` |
|---|---:|---:|
| `rtl_entered` / 原 `rtl_success` | true / true | true / true |
| 飞行后 `landed_state` | 1 | 1 |
| 最终离起飞原点的水平距离 | 48.611 m | 24.607 m |
| 最后HEARTBEAT的armed | true | true |
| 最大位置接收间隔 | 0.293509 s | 0.288754 s |
| PX4 ULog | logger off | logger off |

冻结源 `scripts/smoke_airframe_flight.py` 对此机型的 `rtl_success` 直接使用进入RTL的判据；该结果不是home arrived或精准回家落地。`landed`表示已有飞行后的落地状态信号，不能替代独立实体触地证据，也不包含自动disarm要求。末条心跳仍armed=true，清理前没有取得自动disarm证据；不能据此宣称自动停桨/解除武装已经验证。

另须保留：fixed的普通短时 `reposition_success=false` 是原源码明确不参与通过判定的参考量，严格FW环形重定位真实通过，不能将前者改写为true；云台初始空速告警后prearm ready=true，不等于空速失效保护完成。只有fixed留有独立docker inspect，gimbal以同一冻结launcher及不可变image launch事件作证；两次均无ULog交叉核查。这两次SIM smoke不替代clock10、原21×2或真机保护门禁。

### 既有软件证据与三角翼真机缺口

- [ ] **签名与真实链路**：保留[SIM计划签名审批记录](e2e-operator-acceptance.md)及[本机UDP/HMAC与GR01硬件准入区别](integration-input-checklist.md#d-第四批数传告警)。它们不证明三角翼实际机载机/飞控/GCS的设备身份、密钥部署、重放/过期/错误签名拒绝和断链重连已验收；应另行保存端到端ACK及拒绝事件，报告只含key ID/摘要，不含密钥或凭据。本次630飞行不是签名审批链专项测试。
- [ ] **实体标定与时序**：仿真共享标定和云台小于4°的关节跟踪门禁，不是实装相机/云台标定。按[真实运行入口的标定要求与限制](onboard-runtime-real-aircraft.md#相机和标定)，分别测量MC/FW姿态、固定/云台外参、实际分辨率与畸变、传感器曝光到MAVLink时间误差；保存实装证据，不把Gazebo参数当实体测量。相关[机型档案限制](onboard-airframes.md#限制与未验证项)仍适用。
- [ ] **空中重启与控制权**：[crash-resume记录](crash-resume.md)已有通用PX4/Gazebo SITL五场景历史PASS，不能抹去，也不能挪作630三角翼或真机恢复通过。当前两次630复验没有注入机载进程/设备断电重启，未验证实机MC、FW、转换及RTL/LAND状态下的检查点身份/时效、PX4 boot回退拒绝、恢复后不重复arm/takeoff，以及不得抢回PX4失效保护控制权。后续须在受控台架/HIL和获准实机流程分别取证，不能因本SIM通过而打开接管。

上述新增项均保持未勾选。当前记录不构成真机放行，也不声称已刷写或部署候选到真机。
