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

## 电源、导航、执行器及共用软件

- [ ] 真机导出 `COM_LOW_BAT_ACT`、`BAT_*_THR`、`COM_FLTT_LOW_ACT`、`COM_FLT_TIME_MAX`，校准电池电压/电流；验证低电量、返航能量不足和掉电动作。本轮仿真 `COM_LOW_BAT_ACT=3`，其余未实测。
- [ ] 核对 `GF_ACTION`、围栏边界、`GF_MAX_HOR_DIST` / `GF_MAX_VER_DIST` 及定位丢失动作；零距离默认值不代表已有有效边界。
- [ ] 检查 `CBRK_IO_SAFETY`、`CBRK_SUPPLY_CHK`、传感器校准、arm checks、机体方向、电机顺序/转向、failsafe 输出和停桨开关；拆桨验证后记录签核。
- [ ] 仿真专用 `SENS_EN_MAGSIM=1`、`SENS_GPS0_DELAY=0`、`EKF2_ASP_DELAY=0` 仅描述无延迟仿真传感器，不能直接下发真机；真机保留实测延迟、磁罗盘和空速校准。
- [ ] 重量、重心、惯量、推力、结构与电机温升须有实体测量。现有模型参数来自估计，飞行仿真不能证明结构与推力余量。
- [ ] 固定相机和云台各自验证 RGB 时间戳、机体/云台外参、三轴指令与回读、桨速、图像遮挡。机载与仿真走同一任务策略入口。
- [ ] 现役身份 r21id-r3 + 规划 r24 + 规划前预检保持；新模型仅在完整门槛通过后替换。身份确认独立视点规则亦须在合并后的 21 任务双次基线上复核。
- [ ] 三角翼严格飞行门禁、合并后 21/21 基线和用户要求的双次回归均保存不可混用的输出目录。当前新飞行检查仍运行，不能据此放行 main。
- [ ] 发布前审查私有设计及凭据；原始 CAD/报告留在本地或获授权的私有位置，不新增公开传播。检查记录保存在仓库外 `runs/delta-publish-asset-audit-20261108.json`。

未完成项均保留未勾选；本文不批准实飞、不替代机型适航/操作签核。
