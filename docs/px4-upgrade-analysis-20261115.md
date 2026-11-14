# PX4 main 升级分析（2026-11-15）

结论：可以准备隔离的 main 构建候选，尚不能替换现役运行镜像。两个 lockstep 补丁的目标修复已在目标源码中；gimbal attitude device ID 补丁仍需应用且可原样应用。4050/4051 的 97 项显式参数均有定义，主要兼容阻塞在 airframe 之外：GPS 参数重命名、验收脚本错误的 VTOL circuit breaker 值、ROMFS/参数元数据与新二进制必须成套打包，以及实际飞行和云台行为尚未验证。

## 1. 审计对象与证据边界

- 本文 `U/` 表示只读目录 `/root/localds/px4up/px4-main/`；无 `U/` 前缀的项目路径相对于本 worktree。下表中的 `src/` 和 `ROMFS/` 证据若注明“上游”，均在 U 中。
- 目标是本地提供的 main HEAD `53afcc82b2a51a90424ff7e2c35583e491587eb5`，提交时间 `2026-11-13T17:42:06-05:00`。本次没有联网，因此“main”指这个确定快照，不声称它仍等于远端最新 HEAD。
- 请求的旧树 `/data/scratch/px4-clock-build-20261108` 不存在。无法直接做旧 checkout 与新 checkout 的完整 diff；旧行为比较依据项目保存的补丁 preimage、`docs/px4-lockstep-backport-20261108.md`、旧 airframe 和项目提交。不能据此证明 pin 之后每个 PR 的合入时间。题述 #29608、#29626、#28656 等作为后续回归目标，未从浅克隆推断历史。
- 本次只写本 worktree/临时目录；未构建 PX4、启动容器、使用网络或 GPU、提交、改动上游源码或运行 pin。上游 `git status --short` 为空。
- 结果是源码兼容分析和候选文件；PX4 升级、镜像推广和飞行验收仍待后续执行。

## 2. 三个补丁的决定

实际运行命令（从项目根目录，以三个补丁名分别替换 `<patch>`）：

```bash
git -C /root/localds/px4up/px4-main apply --check "$PWD/simulation/px4/patches/<patch>"
```

| 补丁 | 实测退出码 | 原因与决定 | 新树证据 |
| --- | --- | --- | --- |
| `lockstep-lifetime-r1.patch` | 1，header:91 / cpp:55 不匹配 | **已被目标源码覆盖，不移植、不再应用。** | `U/platforms/posix/src/px4/common/lockstep_scheduler/include/lockstep_scheduler/lockstep_scheduler.h:93`、`:104`；同目录下 `src/lockstep_scheduler.cpp` 的58行、`:109`、`:173` |
| `lockstep-27a21488.patch` | 1，同上 | **生命周期与 fanout 修复已覆盖；不移植其中的墙钟轮询/重等逻辑。** | 同 cpp `:157`–`:193` 明确保留单次 `pthread_cond_wait`，处理调用者外部通知 |
| `gz-gimbal-attitude-device-id.patch` | 0 | **仍需要，原补丁可用。** 无需生成内容重复的 `-main.patch`。 | `U/src/modules/simulation/gz_bridge/GZGimbal.cpp:239`–`:253` 的 attitude 未赋 device ID；information 在 `:231` 已赋值 |

### 2.1 lockstep：修复的具体对应关系

两个项目补丁的旧上下文保留了 `WakeUp wakeups[32]` 和 `_broadcast_mutex`。新树改用 `TimedWait::signal_next` 临时链表，不再有 32 个唤醒的硬上限；`_signaling_mutex` 覆盖完整 set-time 操作，分三阶段收集、广播、清理；等待者返回前释放 caller mutex，再通过 signaling/timed-waits mutex 等待广播阶段完成，防止 caller 栈上 mutex/cond 被提前释放。证据分别是上游 header:93–105、cpp:58–127、cpp:173–193。

原 `27a21488` 的额外轮询不能当作升级必需项：新源码 cpp:157–163 特别解释，重新等待会吞掉 `px4_sem_post` 等调用者发出的通知。main 的单次 native wait 与项目最终 lifetime-r1 的意图一致。上游测试覆盖等待/外部通知并在thread join前显式推进时间清理，见 `U/platforms/posix/src/px4/common/lockstep_scheduler/test/src/lockstep_scheduler_test.cpp:22`、`:65`、`:288`。本次只核查源码，未编译或执行 C++ 测试；后续须执行项目生命周期、fanout40、外部信号三个探针（`simulation/px4/lockstep-validation/lifecycle_regression.cpp:39`–`:92`）。

### 2.2 gimbal：information 正确不等于 attitude 正确

`U/src/modules/simulation/gz_bridge/GZGimbal.hpp:124` 定义设备 ID 为 1。`publishDeviceInfo()` 能应答 REQUEST_MESSAGE 并发布带 ID=1 的信息（cpp:195–235），但 `publishDeviceAttitude()` 将结构体零初始化，未填写 `gimbal_device_id`，仍发 0（cpp:239–253）。上游 MAVLink 输出会学习 information 的 ID 并传给 manager（`U/src/modules/gimbal/output_mavlink.cpp:149`–`:198`），manager status 使用 `device_compid`（`input_mavlink.cpp:463`–`:479`）；这些路径不会修复 attitude 中的 0。

项目 `src/multidetect/gimbal_controller.py:133`–`:183` 按设备 ID 筛选 attitude/所有权，故仍会丢弃该状态。补丁补齐 attitude ID，与 information/manager 保持一致。新测试先对 U 执行 `apply --check`，再只复制 `GZGimbal.cpp` 到 pytest 临时目录，实际应用补丁并检查 ID 赋值，同时确认 U 文件字节未变。body/earth setpoint、rate 积分、MNT_TAU 和 AUX 轴改动并未替代该修复；新角度语义仍要独立做实景回归。

## 3. Airframe 参数、执行器与启动链

新增候选副本 `simulation/px4/airframes-main/4050_gz_delta_tailsitter`、`4051_gz_delta_tailsitter_gimbal`；原目录未改。4050 的 87 项、4051 额外的 10 项显式参数和值全部保留。4051 将模型默认选择移到 source 4050 **之前**，修复未显式传入 `PX4_SIM_MODEL` 时先被基础脚本占用默认值的问题；显式环境覆盖仍保留。顺序与上游 `U/ROMFS/px4fmu_common/init.d-posix/airframes/4019_gz_x500_gimbal:8`–`:10` 一致，新增 shell 执行测试覆盖默认和显式覆盖两种情况。

“unchanged”表示旧名在 main 仍有正式定义，候选保留原值；不表示控制器数值响应与旧版本完全相同。下表逐项给出 **上游参数定义**，不是仅凭另一个 airframe 中出现同名即判定有效。

### 3.1 97 项显式参数

| 旧名及项目值 | main 名称/状态 | 上游定义（U 下） |
| --- | --- | --- |
| `SIM_GZ_EN` = `1` | unchanged | `src/modules/simulation/gz_bridge/parameters.yaml:84` |
| `MAV_TYPE` = `20` | unchanged | `src/modules/mavlink/mavlink_params.yaml:50` |
| `CA_AIRFRAME` = `4` | unchanged | `src/modules/control_allocator/module.yaml:11` |
| `CA_ROTOR_COUNT` = `4` | unchanged | `src/modules/control_allocator/module.yaml:120` |
| `CA_ROTOR0_PX` = `0.141` | unchanged | `src/modules/control_allocator/module.yaml:139` |
| `CA_ROTOR0_PY` = `0.6` | unchanged | `src/modules/control_allocator/module.yaml:150` |
| `CA_ROTOR0_PZ` = `-0.041` | unchanged | `src/modules/control_allocator/module.yaml:161` |
| `CA_ROTOR0_KM` = `0.025` | unchanged | `src/modules/control_allocator/module.yaml:222` |
| `CA_ROTOR1_PX` = `-0.159` | unchanged | `src/modules/control_allocator/module.yaml:139` |
| `CA_ROTOR1_PY` = `-0.6` | unchanged | `src/modules/control_allocator/module.yaml:150` |
| `CA_ROTOR1_PZ` = `-0.041` | unchanged | `src/modules/control_allocator/module.yaml:161` |
| `CA_ROTOR1_KM` = `0.025` | unchanged | `src/modules/control_allocator/module.yaml:222` |
| `CA_ROTOR2_PX` = `0.141` | unchanged | `src/modules/control_allocator/module.yaml:139` |
| `CA_ROTOR2_PY` = `-0.6` | unchanged | `src/modules/control_allocator/module.yaml:150` |
| `CA_ROTOR2_PZ` = `-0.041` | unchanged | `src/modules/control_allocator/module.yaml:161` |
| `CA_ROTOR2_KM` = `-0.025` | unchanged | `src/modules/control_allocator/module.yaml:222` |
| `CA_ROTOR3_PX` = `-0.159` | unchanged | `src/modules/control_allocator/module.yaml:139` |
| `CA_ROTOR3_PY` = `0.6` | unchanged | `src/modules/control_allocator/module.yaml:150` |
| `CA_ROTOR3_PZ` = `-0.041` | unchanged | `src/modules/control_allocator/module.yaml:161` |
| `CA_ROTOR3_KM` = `-0.025` | unchanged | `src/modules/control_allocator/module.yaml:222` |
| `CA_SV_CS_COUNT` = `0` | unchanged | `src/modules/control_allocator/module.yaml:253` |
| `SIM_GZ_EC_FUNC1` = `101` | unchanged | `src/modules/simulation/gz_bridge/module.yaml:14` |
| `SIM_GZ_EC_FUNC2` = `102` | unchanged | `src/modules/simulation/gz_bridge/module.yaml:14` |
| `SIM_GZ_EC_FUNC3` = `103` | unchanged | `src/modules/simulation/gz_bridge/module.yaml:14` |
| `SIM_GZ_EC_FUNC4` = `104` | unchanged | `src/modules/simulation/gz_bridge/module.yaml:14` |
| `SIM_GZ_EC_MIN1` = `0` | unchanged | `src/modules/simulation/gz_bridge/module.yaml:14` |
| `SIM_GZ_EC_MIN2` = `0` | unchanged | `src/modules/simulation/gz_bridge/module.yaml:14` |
| `SIM_GZ_EC_MIN3` = `0` | unchanged | `src/modules/simulation/gz_bridge/module.yaml:14` |
| `SIM_GZ_EC_MIN4` = `0` | unchanged | `src/modules/simulation/gz_bridge/module.yaml:14` |
| `SIM_GZ_EC_MAX1` = `1100` | unchanged | `src/modules/simulation/gz_bridge/module.yaml:14` |
| `SIM_GZ_EC_MAX2` = `1100` | unchanged | `src/modules/simulation/gz_bridge/module.yaml:14` |
| `SIM_GZ_EC_MAX3` = `1100` | unchanged | `src/modules/simulation/gz_bridge/module.yaml:14` |
| `SIM_GZ_EC_MAX4` = `1100` | unchanged | `src/modules/simulation/gz_bridge/module.yaml:14` |
| `THR_MDL_FAC` = `1` | unchanged | `src/lib/mixer_module/motor_params.yaml:5` |
| `MPC_THR_HOVER` = `0.44` | unchanged | `src/modules/mc_pos_control/multicopter_position_control_params.yaml:5` |
| `BAT1_N_CELLS` = `4` | unchanged | `src/lib/battery/module.yaml:58` |
| `BAT1_CAPACITY` = `5000` | unchanged | `src/lib/battery/module.yaml:87` |
| `FD_FAIL_R` = `70` | unchanged | `src/modules/commander/failure_detector/failure_detector_params.yaml:5` |
| `FW_P_TC` = `0.6` | unchanged | `src/modules/fw_att_control/fw_att_control_params.yaml:18` |
| `FW_PR_I` = `0.963310` | unchanged | `src/modules/fw_rate_control/fw_rate_control_params.yaml:37` |
| `FW_PR_P` = `1.605517` | unchanged | `src/modules/fw_rate_control/fw_rate_control_params.yaml:16` |
| `FW_PSP_OFF` = `2` | unchanged | `src/modules/fw_att_control/fw_att_control_params.yaml:137` |
| `FW_RR_FF` = `0.892968` | unchanged | `src/modules/fw_rate_control/fw_rate_control_params.yaml:134` |
| `FW_RR_I` = `0.892968` | unchanged | `src/modules/fw_rate_control/fw_rate_control_params.yaml:76` |
| `FW_RR_P` = `1.785935` | unchanged | `src/modules/fw_rate_control/fw_rate_control_params.yaml:56` |
| `FW_YR_FF` = `0` | unchanged | `src/modules/fw_rate_control/fw_rate_control_params.yaml:162` |
| `FW_YR_I` = `0` | unchanged | `src/modules/fw_rate_control/fw_rate_control_params.yaml:115` |
| `FW_YR_P` = `0.291559` | unchanged | `src/modules/fw_rate_control/fw_rate_control_params.yaml:95` |
| `FW_R_TC` = `1.0` | unchanged | `src/modules/fw_att_control/fw_att_control_params.yaml:5` |
| `FW_PN_R_SLEW_MAX` = `20` | unchanged | `src/modules/fw_lateral_longitudinal_control/fw_lat_long_params.yaml:5` |
| `NPFG_PERIOD` = `20` | unchanged | `src/modules/fw_mode_manager/fw_mode_manager_params.yaml:527` |
| `NPFG_ROLL_TC` = `1.0` | unchanged | `src/modules/fw_mode_manager/fw_mode_manager_params.yaml:562` |
| `FW_THR_TRIM` = `0.32` | unchanged | `src/lib/fw_performance_model/performance_model_params.yaml:5` |
| `FW_THR_MAX` = `0.8` | unchanged | `src/modules/fw_mode_manager/fw_mode_manager_params.yaml:388` |
| `FW_THR_MIN` = `0.05` | unchanged | `src/modules/fw_mode_manager/fw_mode_manager_params.yaml:401` |
| `FW_THR_IDLE` = `0.05` | unchanged | `src/modules/fw_mode_manager/fw_mode_manager_params.yaml:415` |
| `FW_R_LIM` = `35` | unchanged | `src/modules/fw_mode_manager/fw_mode_manager_params.yaml:377` |
| `FW_T_CLMB_MAX` = `6` | unchanged | `src/lib/fw_performance_model/performance_model_params.yaml:92` |
| `FW_T_HRATE_FF` = `0.5` | unchanged | `src/modules/fw_lateral_longitudinal_control/fw_lat_long_params.yaml:146` |
| `FW_T_SINK_MAX` = `3` | unchanged | `src/modules/fw_lateral_longitudinal_control/fw_lat_long_params.yaml:195` |
| `FW_T_SINK_MIN` = `1.6` | unchanged | `src/lib/fw_performance_model/performance_model_params.yaml:106` |
| `FW_AIRSPD_STALL` = `10` | unchanged | `src/lib/fw_performance_model/performance_model_params.yaml:132` |
| `FW_AIRSPD_MIN` = `12` | unchanged | `src/lib/fw_performance_model/performance_model_params.yaml:145` |
| `FW_AIRSPD_TRIM` = `15` | unchanged | `src/lib/fw_performance_model/performance_model_params.yaml:119` |
| `FW_AIRSPD_MAX` = `18` | unchanged | `src/lib/fw_performance_model/performance_model_params.yaml:162` |
| `MC_AIRMODE` = `2` | unchanged | `src/lib/mixer_module/params.yaml:5` |
| `MAN_ARM_GESTURE` = `0` | unchanged | `src/modules/manual_control/manual_control_params.yaml:5` |
| `MC_ROLL_P` = `3` | unchanged | `src/modules/mc_att_control/mc_att_control_params.yaml:5` |
| `MC_PITCH_P` = `3` | unchanged | `src/modules/mc_att_control/mc_att_control_params.yaml:16` |
| `MC_ROLLRATE_P` = `0.3` | unchanged | `src/modules/mc_rate_control/mc_rate_control_params.yaml:5` |
| `MC_PITCHRATE_P` = `0.3` | unchanged | `src/modules/mc_rate_control/mc_rate_control_params.yaml:76` |
| `VT_ARSP_TRANS` = `15` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:81` |
| `VT_ARSP_BLEND` = `10` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:69` |
| `VT_B_DEC_MSS` = `1` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:56` |
| `VT_B_TRANS_DUR` = `10` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:35` |
| `VT_B_TRANS_RAMP` = `5` | unchanged | `src/modules/vtol_att_control/standard_params.yaml:38` |
| `VT_FW_DIFTHR_EN` = `7` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:213` |
| `VT_FW_DIFTHR_S_R` = `1` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:228` |
| `VT_FW_DIFTHR_S_P` = `1` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:238` |
| `VT_FW_DIFTHR_S_Y` = `1` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:248` |
| `VT_F_TRANS_DUR` = `5` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:24` |
| `VT_F_TRANS_THR` = `0.25` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:46` |
| `VT_F_TR_OL_TM` = `8` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:199` |
| `VT_TRANS_TIMEOUT` = `20` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:92` |
| `VT_TYPE` = `0` | unchanged | `src/modules/vtol_att_control/vtol_att_control_params.yaml:5` |
| `WV_EN` = `0` | unchanged | `src/lib/weather_vane/weathervane_params.yaml:5` |
| `EKF2_FUSE_BETA` = `0` | unchanged | `src/modules/ekf2/params_sideslip.yaml:5` |
| `MNT_MODE_IN` = `4` | unchanged | `src/modules/gimbal/gimbal_params.yaml:5` |
| `MNT_MODE_OUT` = `2` | unchanged | `src/modules/gimbal/gimbal_params.yaml:26` |
| `MNT_RC_IN_MODE` = `1` | unchanged | `src/modules/gimbal/gimbal_params.yaml:209` |
| `MNT_MAN_ROLL` = `1` | unchanged | `src/modules/gimbal/gimbal_params.yaml:82` |
| `MNT_MAN_PITCH` = `2` | unchanged | `src/modules/gimbal/gimbal_params.yaml:97` |
| `MNT_MAN_YAW` = `3` | unchanged | `src/modules/gimbal/gimbal_params.yaml:112` |
| `MNT_RANGE_ROLL` = `90` | unchanged | `src/modules/gimbal/gimbal_params.yaml:157` |
| `MNT_MAX_PITCH` = `45` | unchanged | `src/modules/gimbal/gimbal_params.yaml:141` |
| `MNT_MIN_PITCH` = `-135` | unchanged | `src/modules/gimbal/gimbal_params.yaml:149` |
| `MNT_RANGE_YAW` = `720` | unchanged | `src/modules/gimbal/gimbal_params.yaml:168` |

`CA_ROTOR${i}`、`BAT${i}` 是元数据按实例展开的参数。`SIM_GZ_EC_FUNC/MIN/MAX1..4` 由 `gz_bridge/module.yaml:14`–`:28` 的 actuator 配置生成；生成规则在 `U/Tools/module_config/generate_params.py:173`、`:287`、`:309`–`:342`，因此不用寻找不存在的字面 `PARAM_DEFINE(SIM_GZ_EC_FUNC1)`。metadata 的 MAX 上限与项目 1100 存在差异，但上游 4018 也使用 1100（`U/ROMFS/px4fmu_common/init.d-posix/airframes/4018_gz_quadtailsitter:36`–`:49`）；本次不改比例，构建后必须回读并验证 0..1100 的 MAVLink 输出与 Gazebo 转速。

### 3.2 source 继承的参数

4050 source `rc.vtol_defaults`，不是独立完整的启动脚本。该上游文件仍设置 `VEHICLE_TYPE=vtol`（`U/ROMFS/px4fmu_common/init.d/rc.vtol_defaults:8`），下面列出未在上述显式表覆盖的全部继承默认值；MAV_TYPE、EKF2_FUSE_BETA、MC_ROLL_P、WV_EN 最终由 4050 覆盖。

| 参数及上游继承值 | main 名称/状态 | 上游定义（U 下） |
| --- | --- | --- |
| `COM_POS_LOW_EPH` = `50` | unchanged（继承） | `src/modules/commander/commander_params.yaml:716` |
| `MIS_TAKEOFF_ALT` = `20` | unchanged（继承） | `src/modules/navigator/mission_params.yaml:5` |
| `EKF2_ARSP_THR` = `10` | unchanged（继承） | `src/modules/ekf2/params_airspeed.yaml:5` |
| `HTE_VXY_THR` = `2` | unchanged（继承） | `src/modules/mc_hover_thrust_estimator/hover_thrust_estimator_params.yaml:43` |
| `MIS_TKO_LAND_REQ` = `2` | unchanged（继承） | `src/modules/navigator/mission_params.yaml:17` |
| `MPC_ACC_HOR_MAX` = `2` | unchanged（继承） | `src/modules/mc_pos_control/multicopter_position_mode_params.yaml:62` |
| `MPC_VEL_MANUAL` = `5` | unchanged（继承） | `src/modules/mc_pos_control/multicopter_position_mode_params.yaml:21` |
| `MPC_XY_ERR_MAX` = `5` | unchanged（继承） | `src/modules/mc_pos_control/multicopter_autonomous_params.yaml:116` |
| `MPC_XY_VEL_MAX` = `8` | unchanged（继承） | `src/modules/mc_pos_control/multicopter_position_control_limits_params.yaml:5` |
| `MPC_JERK_MAX` = `4.5` | unchanged（继承） | `src/modules/mc_pos_control/multicopter_position_mode_params.yaml:75` |
| `MPC_YAW_MODE` = `4` | unchanged（继承） | `src/modules/mc_pos_control/multicopter_autonomous_params.yaml:5` |
| `MC_ROLLRATE_MAX` = `120` | unchanged（继承） | `src/modules/mc_att_control/mc_att_control_params.yaml:54` |
| `MC_YAW_P` = `2` | unchanged（继承） | `src/modules/mc_att_control/mc_att_control_params.yaml:27` |
| `MC_YAWRATE_MAX` = `120` | unchanged（继承） | `src/modules/mc_att_control/mc_att_control_params.yaml:88` |
| `MPC_MAN_Y_MAX` = `90` | unchanged（继承） | `src/modules/mc_pos_control/multicopter_stabilized_mode_params.yaml:15` |
| `RTL_TYPE` = `1` | unchanged（继承） | `src/modules/navigator/rtl_params.yaml:51` |
| `SYS_HAS_NUM_ASPD` = `1` | unchanged（继承） | `src/lib/systemlib/system_params.yaml:180` |

继承值来自目标 main 自身，旧 checkout 缺失，不能证明这些继承默认值与 pin 完全一致。新二进制必须配套本次 main 的启动脚本和参数，验收时记录完整参数快照作比较。

### 3.3 mixer、actuator、模块与 init 命令逐项核查

| 项目用法 | main 核查结果与证据 |
| --- | --- |
| `#!/bin/sh`；`. ${R}etc/init.d/rc.vtol_defaults`；`param set-default` | main 仍采用同类 POSIX ROMFS 脚本与参数默认设置。`U/ROMFS/px4fmu_common/init.d-posix/airframes/4018_gz_quadtailsitter:10`、`U/ROMFS/px4fmu_common/init.d/rc.vtol_defaults:8`–`:43`。脚本 `sh -n` 通过；实际 PX4 shell 行为留给启动验收。 |
| `PX4_SIMULATOR=gz`、`PX4_GZ_WORLD=default`、`PX4_SIM_MODEL=delta_tailsitter`；4051 source 4050 并选择 gimbal model | 仍采用同类环境模型选择路径，见上游 4018、4019 与 `U/ROMFS/px4fmu_common/init.d-posix/px4-rc.gzsim:155`、`:212`。项目 wrapper 名称由模型挂载提供。候选已修正 4051 默认赋值顺序。 |
| 四旋翼 tailsitter、无舵面；无 `.mix` 文件或 legacy mixer 启动命令 | `CA_AIRFRAME=4` 为 tailsitter、`CA_ROTOR_COUNT=4`、`CA_SV_CS_COUNT=0` 均有效；`U/src/modules/control_allocator/module.yaml:11`、`:120`、`:253`。这是 control allocation 路径。 |
| 四个电机位置/反扭矩 KM | 元数据定义有效；KM 正=CCW、负=CW（同 module.yaml:222）。必须保留项目电机顺序和 Gazebo rotor topic 对应，不能因迁移重排。 |
| `SIM_GZ_EC_FUNC1..4=101..104`、MIN=0、MAX=1100 | `U/src/lib/mixer_module/output_functions.yaml:10`–`:12` 定义 Motor1 起点 101；`U/src/modules/simulation/gz_bridge/GZMixingInterfaceESC.cpp:66`–`:104` 依序发布已配置输出，非 motor 的符号调整不适用于这里的四路 motor。 |
| gimbal 采用 `MNT_MODE_IN=4 / OUT=2` | `U/src/modules/gimbal/gimbal_params.yaml:5`、`:26` 与上游 4019:13–15 相同。AUX 输出 420/421/422（`output_functions.yaml:33`–`:35`）只在相应 AUX 路径需要配置，当前 Gazebo gimbal 不应为追随 AUX 改动盲目新增 actuator 函数。min/max 参数说明虽含 AUX，Gazebo 也读取对应范围。 |
| airframe 本身直接启动的模块 | **无**。4050/4051 只 source/default/param；模块由 `rcS -> rc.vehicle_setup -> rc.vtol_apps` 启动。 |
| VTOL 运行模块 | `U/ROMFS/px4fmu_common/init.d/rc.vtol_apps:11`–`:46` 逐项为 `control_allocator start`、`airspeed_selector start`、`vtol_att_control start`、`mc_rate_control start vtol`、`mc_att_control start vtol`、`flight_mode_manager start`、`mc_pos_control start vtol`、`mc_hover_thrust_estimator start`、`fw_rate_control start vtol`、`fw_att_control start vtol`、`fw_mode_manager start`、`fw_lat_lon_control start vtol`、`fw_autotune_attitude_control start vtol`、`land_detector start vtol`。`MC_AT_EN>0` 时启动 `mc_autotune_attitude_control`，`VTE_EN>0` 时启动 `vision_target_estimator`。候选不覆盖这一整条上游链。 |
| 通用 SITL 初始化 | `U/ROMFS/px4fmu_common/init.d-posix/rcS:267`–`:295` 启动条件 failure manager、battery/system-power 模拟、tone、rc_update、manual_control、sensors、commander、pwm_out_sim、vehicle_setup、navigator；`:330`–`:332` 按 MNT_MODE_IN 启动 gimbal；`:359` 进入 MAVLink 脚本；`:371`–`:373` 最后异步启动 ekf2。`px4-rc.gzsim:223` 按 SENS_EN_MAGSIM 启动磁模拟。不要把“收到 heartbeat”当作 estimator 已稳定。 |

`VT_B_TRANS_RAMP` 名称有效，但它定义在standard_params，消费位于`U/src/modules/vtol_att_control/standard.h:87`，不是tailsitter专用回转换控制量；本次保留历史值，不能据该参数存在就认定会调节4050行为。

main 默认仍各开一个 IMU/MAG/BARO/GPS（`U/src/modules/simulation/gz_bridge/parameters.yaml:32`、`:51`、`:61`、`:72`），没有发现 4050/4051 必须新增的传感器数量参数。`SIM_GZ_EN_GPS` **仍叫 GPS**，不能做全局字符串替换。模块是否已被具体 SITL 配置编入，要在后续构建与 shell status 中确认。

## 4. Harness 参数与 GPS→GNSS 迁移

检索覆盖 `scripts/`、`src/multidetect/` 的 `param set`、`px4-param`、`PARAM_SET`、`PX4_PARAM_`、`param_`、`parameters.bson`、启动 `-p` 及档案中的 `px4_name`。没有找到另一个隐藏的项目固定 BSON 参数覆盖清单；`-p` 命中还包含 gdb attach/非 PX4 参数开关，不能当作参数名。collector 接受开放的 `extra_sim_params`，通用 MAVLink PARAM_SET 也可接受用户运行时输入，故不可能静态枚举所有未来输入，需对每次实际计划生成参数清单并回读。

### 4.1 重命名/移除/保留表

以下除特别注明，证据均在 U；`n=0,1` 表示两套实例。

| 旧名/项目依赖 | main 名称或状态 | 新树 file:line 证据与处理 |
| --- | --- | --- |
| `SENS_GPS0_DELAY=0`（launcher） | **`SENS_GNSS0_DELAY=0`** | `src/lib/parameters/param_translation.cpp:397`；正式定义 `src/modules/sensors/vehicle_gps_position/module.yaml:72`，默认 110 ms。main 专用 harness 必须改名并回读 0。 |
| `SENS_GPS1_DELAY` | `SENS_GNSS1_DELAY` | translation:402、同定义 :72。 |
| `SENS_GPSn_ID` | `SENS_GNSSn_ID` | translation:393、:398；定义 module.yaml:20。导航参考和 checkpoint 参数读取必须一起适配。 |
| `SENS_GPSn_OFFX/OFFY/OFFZ` | `SENS_GNSSn_OFFX/OFFY/OFFZ` | translation:394–396、:399–401；定义 module.yaml:33/:46/:59。 |
| `SENS_GPS_PRIME` | `SENS_GNSS_PRIME` | translation:392；定义 module.yaml:8，主接收机选择语义亦需核查。 |
| `EKF2_GPS_DELAY`（旧参数导入兼容） | `SENS_GNSS0_DELAY`、`SENS_GNSS1_DELAY` | translation:203–209；不是设置旧 shell 名的通用别名。 |
| `EKF2_GPS_POS_X/Y/Z`（旧参数导入兼容） | `SENS_GNSS0_OFFX/Y/Z` | translation:182–200。 |
| `EKF2_GPS_CHECK` | `GNSS_CHECK` | translation:417；正式定义 module.yaml:88。 |
| `EKF2_REQ_EPH/EPV/NSATS/PDOP/HDRIFT/VDRIFT/FIX/SACC` | `GNSS_REQ_EPH/EPV/NSATS/PDOP/HDRIFT/VDRIFT/FIX/SACC` | translation:418–425；module.yaml:112–182。 |
| `EKF2_REQ_GPS_H` | `GNSS_REQ_TIME` | translation:426；module.yaml:197。 |
| `EKF2_GPS_CTRL`、`MODE`、`P_NOISE`、`P_GATE`、`V_GATE`、`V_NOISE`、`YAW_RST` | **unchanged**，不是整组更名 | `src/modules/ekf2/params_gnss.yaml:5/:20/:30/:39/:49/:59/:81`。CTRL 仍 bit3=双天线 heading，默认 7 不开该 bit。 |
| `EKF2_ASP_DELAY=0` | unchanged | `src/modules/ekf2/params_airspeed.yaml:17`。 |
| `SENS_EN_MAGSIM=1` | unchanged | `src/modules/simulation/sensor_mag_sim/parameters.yaml:5`。 |
| `CAL_ACC0..2_ID`、`CAL_GYRO0..2_ID`、`CAL_MAG0..1_ID` | 名称保留；启动校准策略需使用配套 rcS | `src/modules/sensors/module.yaml:10/:237/:360`；`ROMFS/px4fmu_common/init.d-posix/rcS:143`–`:155` 设置 SYS_AUTOCFG_CAL=1，IMU ID 1310988/1310996/1311004，MAG 197388/197644。不要直接沿用旧 etc 或持久参数数据库。 |
| `EKF2_IMU_POS_X/Y/Z`、`EKF2_HGT_REF`、`EKF2_EV_CTRL`（导航参考读取） | unchanged | `src/modules/ekf2/module.yaml:136/:144/:152/:87`、`params_external_vision.yaml:5`。 |
| `SIM_GZ_EN_GPS`、`SIM_GZ_EN_ODOM`（导航参考读取） | unchanged | `src/modules/simulation/gz_bridge/parameters.yaml:51/:42`。 |
| `COM_RC_IN_MODE=4`（collector），AI acceptance 还可写 1 | unchanged | `src/modules/commander/commander_params.yaml:62`；项目 collector:823、AI acceptance:413。 |
| `COM_ARM_HFLT_CHK=0`、`COM_MODE_ARM_CHK=0`、`NAV_DLL_ACT=0/1`、`COM_DL_LOSS_T=5` | 名称保留 | `src/modules/commander/commander_params.yaml:586/:793/:404/:5`；实际 enum、默认值与容器隔离需回归。 |
| `CBRK_VTOLARMING=22027`（若干验收脚本） | 名称保留，**22027 不能启用此 breaker** | `src/lib/circuit_breaker/circuit_breaker_params.yaml:61`–`:72`、`circuit_breaker.h:57` 的 key=159753。不能只因 `param set` 成功就判生效，也不应自动改成159753绕过检查；优先在 MC 模式正常解锁。 |
| `COM_ARM_WO_GPS`（operator 读取） | unchanged | `src/modules/commander/commander_params.yaml:107`，不是 COM_ARM_WO_GNSS；取值 0/1/2 仍应按 enum 解释。 |
| `COM_RCL_EXCEPT`、`NAV_RCL_ACT`、`COM_RC_LOSS_T`（operator 读取） | unchanged | 同 commander_params.yaml:441/:420/:27。 |
| `SYS_AUTOSTART=4050/4051` | unchanged，自定义 ID 必须安装相应脚本 | `src/lib/systemlib/system_params.yaml:5`；项目 `src/multidetect/simulator/stack.py:1566`–`:1581`。 |
| `SDLOG_BACKEND=0`、`SDLOG_MODE` | unchanged | `src/modules/logger/module.yaml:29/:16`；launcher:777 注入 backend，验收需另开有 ULog 的运行。 |
| `FW_LAUN_DETCN_ON=1`、`FW_LAUN_AC_THLD=30`、`FW_LAUN_AC_T=0.05` | unchanged | `src/modules/fw_mode_manager/fw_mode_manager_params.yaml:304`；`src/modules/fw_mode_manager/launchdetection/launchdetection_params.yaml:5/:16`。 |
| `FW_LND_ABORT=0`、`FW_LND_USETER=0` | unchanged | fw_mode_manager_params.yaml:222/:74；collector 从档案读取，delta 两档未单列这类 override。 |
| `RWTO_TKOFF=0/1`、`RWTO_ROT_AIRSPD=12` | unchanged | `src/modules/fw_mode_manager/runway_takeoff/runway_takeoff_params.yaml:5/:56`。 |
| `MIS_TAKEOFF_ALT=3`、`MIS_TKO_LAND_REQ=1`、`MIS_LND_ABRT_ALT` | unchanged | `src/modules/navigator/mission_params.yaml:5/:17/:90`。 |
| `SIM_BAT_DRAIN=300` | unchanged | `src/modules/simulation/battery_simulator/battery_simulator_params.yaml:5`。 |
| `FW_THR_MIN`（overspeed 注入0.45，随后恢复0.05） | unchanged | fw_mode_manager_params.yaml:401；collector:7000/:7060。候选初值仍0.05。 |
| `MNT_* / VT_* / FW_* / CA_*`（4050/4051） | 逐项 unchanged | 见上方 97 行；没有在候选中删除参数。 |

### 4.2 真正会影响启动/证据的项目位置

1. `scripts/run_simulator_docker.py:101`–`:105` 定义旧 `SENS_GPS0_DELAY`；`:769` 转成 `PX4_PARAM_*`。main `U/ROMFS/px4fmu_common/init.d-posix/rcS:248`–`:255` 直接运行 `param set`，不会走 BSON 导入翻译。`U/src/systemcmds/param/param.cpp:927`–`:937` 找不到旧参数时会打印错误，但普通调用可以返回 0。**这是延迟0覆盖静默失效风险**，会回落到 main 的110ms默认。必须采用版本专用名称并以 `param show`/PARAM_VALUE 实值验收，不能仅检查退出码。
2. 导航参考 `src/multidetect/autonomy/sim_navigation_reference.py:115`–`:131` 仍读取 SENS_GPSn offsets/ID；`teacher_live_navigation_checkpoint.py:922` 也检查旧 ID 名。这会导致 live navigation 证据缺字段或拒绝，不是 airframe 参数表通过就可忽略的问题。
3. `scripts/collect_sim_teacher_episodes.py:3831`–`:3876` 组合 launch、landing 与开放 extra_sim_params；`:3927`–`:3937` 仍从旧4050读 FW_THR_MIN。main 专用执行路径须绑定候选 airframe 的内容/hash；现在相同数值不会暴露这个来源错误，未来改参数会暴露。
4. `scripts/vtol_sitl_scenario_acceptance.py:894`–`:899`、`vtol_sitl_attached_acceptance.py:181`–`:185`、`vtol_sitl_mission_acceptance.py:2913`–`:2916`、`run_simulator_docker_ai_acceptance.py:404`–`:407` 都含 breaker22027。该值在 main 不具预期意义；旧树缺失，不能声称它一定是此次升级才引入的问题。
5. PowerShell auto mission:241–245、datalink loss:381–387 的固定覆盖如上表；datalink loss:370–371 还硬断言旧默认 COM_DL_LOSS_T=10、NAV_DLL_ACT=0，需在候选上重新核实，不能按旧基线假定。
6. launcher:703–721 仍挂载 **旧** `simulation/px4/airframes/`。本次候选副本不会自动生效；后续须做隔离 main 配置路径，并在 plan/metadata 中记录实际 mounted 两个文件的 hash。本次没有改当前 pinned harness。
7. launcher:108–112 移除 Gazebo magnetometer system 并使用 SENS_EN_MAGSIM；:118–123 对 `px4-rc.gzsim` 的 ATTEMPTS=30 做180次重试补丁。main `px4-rc.gzsim:84` 仍有该行，文字补丁目前兼容；必须连同 main rcS 一起运行，不能用旧 ROMFS 掩盖启动差异。

## 5. FW idle thrust 0.05 与 gz_bridge

项目提交 `1e535a87d8382bf216df07f8e326e076b5f1df4a` 增加了 `FW_THR_IDLE=0.05` 与解释；`FW_THR_MIN=0.05` 在该提交前已经存在。其直接动机是低推力被旧 FW rate controller 转成 NaN，污染 tailsitter 回转换交接，产生四电机停转。

main `U/src/modules/fw_rate_control/FixedwingRateControl.cpp:419`–`:442` 对有限的零推力直接传递，仅非有限 setpoint 变成 NaN，原≤2%→NaN路径已经消失。`U/src/modules/vtol_att_control/vtol_type.cpp:112` 保存 FW 推力；`tailsitter.cpp:334` 映射到 tailsitter 推力，`:361`–`:364` 回转换最初50ms仍沿用最后 FW 推力。因此**为规避该特定 NaN 来源而强制 idle>2%的理由已被上游消除**，但有限0仍是0；零推力的差动控制余量、配平和回转换掉高并未由静态检查证明合格。

本次保留 `FW_THR_MIN=FW_THR_IDLE=0.05`，不做飞行调参。待 main 完整基线通过，再独立比较保留值与降低 idle 的低油门/过速/回转换结果。不能把“移除 cutoff”解释为允许删除所有停机保护：`U/src/modules/vtol_att_control/standard.cpp:340`–`:341` 标准 VTOL pusher 仍有0.02→NaN路径，4050 的 VT_TYPE=0 tailsitter 不走该路径。

gz_bridge 的下面各项与 FW rate cutoff 是独立问题，**均不因该 upstream fix 而过时**：

| 变化/项目处理 | 判断与 main 证据 |
| --- | --- |
| attitude device ID 补丁 | 仍需，见第2节；与 motor cutoff 无关。 |
| 四路 rotor/Gazebo actuator 映射 | 保留。`U/src/modules/simulation/gz_bridge/GZMixingInterfaceESC.cpp:66`–`:104` 的输出顺序仍须与模型一致。 |
| 多传感器 | `GZBridge.hpp:149`–`:184` 支持3 IMU/2 MAG/2 BARO/2 GNSS；`GZBridge.cpp:227`–`:258` 和`:426`–`:455` 使用首实例原topic及后续索引后缀。现有模型通常只有首实例，不应把参数开到多实例后容忍缺失topic。检查 device ID、instance 与 CAL_ID 绑定。 |
| IMU/lidar 坐标 | `GZBridge.cpp:602`–`:625` 做 IMU FLU→FRD；`:924`–`:970` 涉及 lidar sensor/body orientation。需要与 Gazebo truth 在不同姿态对比，CPU资产测试只能验证项目模型几何。 |
| 磁场单位/方向与现有 magsim workaround | `GZBridge.cpp:526`–`:565` 区分 tesla/FLU 新模式和 gauss/NED legacy；注释要求 gz-sim≥8.6 且 Magnetometer system 的 use_units_gauss/use_earth_frame_ned=false。当前 upstream server.config 裸 plugin 配置见 `U/Tools/simulation/gz/server.config:12`。legacy 分支仍警告 heading wrong；不可据此认定历史倾斜/转弯偏航问题已解决。保留项目 magsim与移除插件，未来单独做有姿态groundtruth的A/B，再决定启用新模式。gz-rendering8.3.0 与 gz-sim8.6 是不同包版本，不能混为一谈。 |
| GNSS/ESC failure injection | `GZBridge.cpp:913` 的 GNSS process、`GZMixingInterfaceESC.cpp:148`–`:149` 的 ESC process 与传感器/电机故障有关，继续保留并验证恢复、时间推进、quadchute/failsafe状态。 |
| GNSS heading 拆分 | `U/src/modules/sensors/vehicle_gps_position/VehicleGPSPosition.cpp:81`–`:83`、`:247`–`:277`、`:414`–`:434` 分别发布 vehicle_gnss/vehicle_gnss_heading；`U/src/modules/ekf2/EKF2.hpp:496`、`:508` 分别订阅。单GNSS模型不应为了补齐topic伪造双天线heading；若未来启用CTRL bit3，必须增加真实heading源与失效回归。 |

## 6. MAVLink、uORB 与项目内部耦合风险

| 风险/源码依赖 | main 证据与影响 | 能发现问题的测试/验证 |
| --- | --- | --- |
| system ID 扩展到32bit | `U/msg/versioned/VehicleCommand.msg:245`、`:247`、`VehicleCommandAck.msg:38`、`VehicleStatus.msg:105`。`U/src/modules/mavlink/mavlink_params.yaml:5`–`:20` 允许MAV_SYS_ID到2147483647，>255要求全链路支持MAVLink2扩展，gimbal等payload内sysid仍≤255。`mavlink_main.cpp:407`–`:414`、`:610`–`:623` 启用相应扩展。 | 当前候选固定sysid=1、控制端245/255。现有测试只证明8bit范围；若扩展>255，须新增真实编码/解码与路由、ACK/mission、PARAM、gimbal全链测试。 |
| 项目8bit范围/手工wire偏移 | `src/multidetect/pixhawk.py:169`–`:178`、`:329`–`:357`；`pixhawk_parameters.py:63`–`:70`；`jetson_profile.py:183`–`:189`限制1..255。`simulator/live_ai_bridge.py:437`–`:470`、`:632`–`:649`、`px4_gazebo_qgc_router.py:44`–`:61`、`:175`–`:225`、`:321`–`:367`、`qgc_readonly_bridge.py:151`–`:171`包含来源筛选/手工frame解析。 | `tests/test_pixhawk.py`、`test_pixhawk_covariance_binding.py`、`test_px4_runtime_capture.py`、`test_simulator_interactive_router.py`覆盖现有协议；后者含loopback socket，本次未运行。main不自动让项目支持>255。 |
| GPS_RAW_INT/GPS2_RAW 与GLOBAL_POSITION | 上游流名仍叫 `GPS_RAW_INT`/`GPS2_RAW`（`U/src/modules/mavlink/streams/GPS_RAW_INT.hpp:48`、`GPS2_RAW.hpp:48`），内部改订阅 sensor_gnss，依sensors_status_gnss选择实例（两文件:62–95）。`vehicle_gps_position/module.yaml:8`–`:11` 明确primary与GPS_RAW_INT的关系。项目 `pixhawk.py:950`–`:1044`、`:1204`–`:1308` 解码GNSS/global/local，`mavlink_sensor_observer.py`还识别GPS2。 | `test_mavlink_sensor_observer.py`、`test_pixhawk.py`、导航参考/采集测试可发现字段问题；多GNSS必须实测主备切换，不得把GPS2缺失用于单GNSS失败判定。检查GPS_GLOBAL_ORIGIN/HOME_POSITION与local reference同一epoch。 |
| COMMAND_LONG、ACK及控制链 | pixhawk.py:1374–1467用511/512请求流；router发400/3000；`onboard_runtime/vehicle.py:776`–`:819`发送PARAM_SET并回读。main的宽sysid不改变当前1号链，但扩展frame/ACK归属与消息频率会影响手工解析。 | `test_px4_runtime_capture.py`、`test_pixhawk.py`和真实SITL arm/mode/VTOL/mission/parameter回读；需确认ACK命令、目标、结果和实际状态都匹配，不能只等任意ACK。 |
| gimbal协议与角度语义 | `gimbal_controller.py:14`–`:20` 定义1000/1001和285，`:133`–`:183`检查ID/owner；上游 `GZGimbal.cpp:142`–`:186` 的body/earth处理与`:269`开始的rate积分有变化。`GZGimbal.cpp:68`–`:93`还要求MNT_MODE_OUT=2及检测到关节；MAVLink stream在`U/src/modules/mavlink/streams/GIMBAL_DEVICE_ATTITUDE_STATUS.hpp:96`原样转发device ID，不能依靠发送端填补0。 | `test_gimbal_controller.py`、`test_gimbal_manager_session.py`、`test_px4_gimbal_device_id_patch.py`及新增source测试；实景还需device information/manager/attitude都ID1、owner与ACK一致，并在spawn yaw=0/45/90度验证body/earth角和rate命令。 |
| uORB listener文本耦合 | `autonomy/sim_navigation_reference.py:233`–`:317`严格解析ASCII、NED坐标、valid/reset/ref字段；`:805`–`:832`固定 `px4-listener vehicle_local_position -i 0 -n 1`。checkpoint `teacher_live_navigation_checkpoint.py:761`–`:795`还读parameter_update。main `U/msg/versioned/VehicleLocalPosition.msg:6`–`:57`和`:87`保留这些字段/topic；`U/msg/ParameterUpdate.msg:1`、`SensorCombined.msg:1`、`msg/versioned/VehicleStatus.msg:1`仍存在。消息源文件迁至versioned目录不等于shell topic改名。 | `test_sim_navigation_reference.py`、`test_px4_clock_stall_capture.py:140`–`:171`等解析测试；必须捕获main的真实listener文本重新喂解析器。version变化/输出格式/字段值范围不能靠旧fixture证明。 |
| 时钟诊断/启动顺序 | `scripts/px4_clock_stall_capture.py:612`–`:624`抓work_queue/status、sensor_combined、vehicle_status。main native wait虽然源码吻合，仍可能有其它gz启动/传感器等待问题。 | `test_px4_clock_stall_capture.py`验证命令/捕获流程；后续heartbeat、uORB、Gazebo时间连续推进及暂停/恢复、重启、故障注入实测。 |
| runtime identity与teacher证据 | runtime contract仍pin旧83c4及binary SHA `9c2280e0...`；新main不能冒用旧来源/hash。当前launcher仍挂旧airframe，严格teacher链还校验源码/hash。 | `tests/test_px4_runtime_image.py`及相关严格runtime/teacher binding测试；需要单独candidate manifest/资格记录，保持现役contract不变。旧receipt不证明新main。 |

检索未发现项目直接执行 `listener sensor_gps` 的固定命令；已找到的shell listener topic不需机械GPS改名。外部脚本/人工命令以及未来新增多传感器读者仍需检查 `sensor_gnss`、`vehicle_gnss`、`vehicle_gnss_heading` 和 `sensors_status_gnss`。上述现有CPU测试多数使用项目fixture/mock，不能独立证明新PX4运行时兼容。

## 7. 后续构建与验证计划（本次未执行）

以下为主机维护者后续独立执行的步骤。先完成main专用启动适配，再运行实景命令；当前launcher仍挂旧airframe且注入旧GPS名，单独设候选image不能构成有效验证。

### 7.1 准备可审计的隔离构建

1. 将提供的main快照和已checkout子模块复制到新的可写 `/data/scratch/px4-main-upgrade-<stamp>/`，不在U内应用任何补丁。记录HEAD、`git submodule status --recursive`、源码clean状态、三个项目补丁SHA、两份candidate airframe SHA。若要恢复旧树作A/B，由主机另外准备83c4快照；本任务没有获取它。
2. 仅应用原 `gz-gimbal-attitude-device-id.patch`；两个lockstep补丁不应用。保存实际diff并限制变更为该ID赋值；将airframes-main装入候选ROMFS。单独准备main harness配置：GNSS名字及读回、候选airframe挂载/hash、nav reference参数读取、正常MC arming、严格新镜像身份。保留pin版启动路径供对照。
3. 用 `simulation/px4/clock-build/Dockerfile.toolchain` 作为编译容器起点，显式指定已核实的不可变基底，唯一新tag（例如 `multidetect/px4-main-toolchain:53afcc82-<stamp>`）。该Dockerfile:1硬编码旧445基底，apt/pip依赖未锁版本；必须保存实际编译器/包清单和image ID。默认8 CPU、最多12 CPU、64GiB memory及memory+swap、无GPU。构建所需包如需获取，应由主机在后续工作中准备，本次无网络操作。
4. 在该**编译容器**中挂载可写源码到`/src`，项目只读到`/repo`，执行（不复用已有build目录）：

   ```bash
   export GZ_DISTRO=harmonic
   cmake -S /src -B /src/build/px4_sitl_default -G Ninja \
     -DCONFIG=px4_sitl_default -DCMAKE_BUILD_TYPE=RelWithDebInfo \
     -DPYTHON_EXECUTABLE=/opt/px4-build-venv/bin/python
   cmake --build /src/build/px4_sitl_default --target px4 -j8
   ```

   确认配置输出确实启用gz_bridge（不能把缺gz依赖时的fallback当成功，见`U/src/modules/simulation/gz_bridge/CMakeLists.txt:34`–`:58`），并在产物中检查模块入口。先运行上游lockstep独立测试并重复10次；在可写副本适配项目 `simulation/px4/lockstep-validation/` 编译指令后执行 lifecycle/fanout40/external-signal，三个探针均退出0、无超时/死锁。原 `scripts/validate_px4_lockstep_backport.sh` 用旧base/upstream三方比较，不能在浅main上原样调用。不能以Python源码字符串检查代替这些并发测试。
5. **另写候选runtime打包配置**。`simulation/px4/clock-build/Dockerfile.runtime:4` 仅COPY二进制，适合旧pin小补丁，不足以证明main升级一致性。必须将main生成的`bin/px4`、对应ROMFS/etc（包括init.d、init.d-posix及参数/消息相关生成产物）、命令入口/软链接成套安装到现有入口约定的`/opt/px4-gazebo/`；实际产物目录以该构建输出为准并逐项manifest，不直接混用旧镜像etc。保留现役gz-rendering830运行基底的transport库、world/model资产和entrypoint需要的路径，检查main依赖的gz-sim/API与基底实际包版本；必要时升级容器依赖并重新记录整个runtime身份。
6. 新候选使用唯一tag，例如 `multidetect/px4-sitl-gazebo:px4-main-53afcc82-<stamp>`，拒绝覆盖现役tag。执行`ldd`无not-found、PX4版本显示目标HEAD、param/listener/status命令入口可运行。导出新image ID、rootfs/config、二进制/ROMFS/airframe/补丁hash以及工具链清单，生成**独立候选manifest**，不修改 `simulation/gz-transport/runtime-image-contract.json` 的旧pin去迁就候选。

现有 `scripts/build_px4_lockstep_candidate.sh` 硬编码83c4、27a21488、patch hash和旧binary hash，还包含git fetch；因此**不能直接拿它构建main**，也不能靠降低hash检查绕过去。新的候选构建流程需按上述清单实现并复核。

### 7.2 启动与协议烟测：先无GPU，后真实渲染

1. 在独占SITL槽位启动候选运行容器，首先无GPU、无相机或可用的软件渲染，记录实际运行image ID和源码/模型身份。每次使用新参数目录，禁止无审计复用旧`parameters.bson`。若专测旧数据库导入，另建一次性副本，检查translation后的每项值。
2. PX4启动不得有unknown-param、缺模块、缺sensor topic、device-ID碰撞或lockstep停顿。回读全部97项显式参数和继承默认值，特别是SENS_GNSS0_DELAY=0、EKF2_ASP_DELAY=0、SENS_EN_MAGSIM=1、MNT_MODE_OUT=2、CAL_ID、四路MAX=1100、MAV_SYS_ID=1。parameter写入必须匹配读回，不能仅以退出0判成功。
3. 收集heartbeat（现有scenario等待45s，GPS fix20s，见 `scripts/vtol_sitl_scenario_acceptance.py:886`–`:889`），继续等待导航稳定。抓取vehicle_local_position、parameter_update、sensor_combined、vehicle_status原始listener文本并喂项目解析器；验证时间单调、valid/ref/reset字段齐全、LOCAL/GLOBAL/HOME/ORIGIN坐标与epoch一致；Gazebo暂停/恢复、冷启动/连续重启后不挂起。
4. 核对MAVLink COMMAND_LONG arm/mode/VTOL/request-message ACK及实际状态，PARAM_SET/PARAM_VALUE类型和值一致；保持sysid1作兼容基线。GPS_RAW_INT流存在；单GNSS不强求GPS2，双GNSS专项再核实主备选择、failover、恢复、heading拆分。故障注入运行需明确设置并回读SYS_FAILURE_EN=1（`U/src/modules/failure_injection_manager/failure_injection_manager_params.yaml:5`；`U/ROMFS/px4fmu_common/init.d-posix/rcS:267`–`:269`），然后故障注入测试应同时确认输出、mode、失败标志和时钟进展，避免只看某条event。
5. 云台先请求GIMBAL_DEVICE_INFORMATION，观察information/manager/attitude的device ID均为1；配置manager ownership、下发pitch=-45/yaw=0并对照Gazebo三关节角度和带时间戳状态。另测试body/earth绝对角、rate命令/MNT_TAU、失去ownership及重获控制；spawn yaw用0/45/90度覆盖坐标差异。前者有既有严格4度门限，后者为本次新增实景专项，须先定义期望坐标/轨迹，不能以旧fixture自动判通过。
6. 完成以上启动烟测后，在有GPU的**候选运行容器**执行真实相机/depth和完整航迹；编译容器不承担渲染验证。先跑x500/x500_gimbal基线，再standard_vtol参考和delta fixed/gimbal，避免把共用协议/相机失败误判为delta参数问题。

### 7.3 严格delta fixed/gimbal与其它回归命令

完成上述main专用launcher适配及候选身份登记后，依次执行，每次独占SITL锁、独立输出目录/端口；以下是未来命令模板：

```bash
export MULTIDETECT_SIM_IMAGE='<candidate-immutable-image-id>'
export MULTIDETECT_SIM_LISTEN_HOST='<validated-reachable-host-address>'
PYTHONPATH=src:. /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/smoke_airframe_flight.py --airframe delta_tailsitter_mono_cam \
  --out /data/scratch/px4-main-validation-<stamp>/fixed \
  --container-prefix md-main-fixed-<stamp> --control-port 14540 --telemetry-port 14541
PYTHONPATH=src:. /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/smoke_airframe_flight.py --airframe delta_tailsitter_gimbal_cam \
  --out /data/scratch/px4-main-validation-<stamp>/gimbal \
  --container-prefix md-main-gimbal-<stamp> --control-port 14542 --telemetry-port 14543
```

同一脚本分别使用 `--airframe x500_gimbal`、`--airframe standard_vtol_mono_cam` 执行协议/云台与标准VTOL参考（保持输出目录唯一）。固定翼另执行仓库已有rc_cessna runway/hand/catapult适用档案，验证TECS、launch和autoland；“无flare”需构造明确关闭/不可进入flare的附加场景，不能从delta垂直降落通过推断固定翼autoland修复。

针对题述tailsitter前转换重启和quadchute修复，增加 **MC→FW前转换中止→MC稳定→再次前转换→FW→回MC** 场景；不同起始航向重复，检查FW heading、transition状态重置、超时与quadchute event原因。外部/故障触发quadchute必须回到受控MC并可按预期恢复，不能把quadchute本身误等同不可恢复的vehicle failure。现有完整smoke没有覆盖所有这种中止重启分支，这些专项通过前保留开放风险。

### 7.4 通过/失败标准：沿用现有严格判定

不要为了适配main放宽这些阈值。以下取自 `scripts/smoke_airframe_flight.py` 及collector/metric代码。

| 项目 | 必须满足 | 现有判定位置 |
| --- | --- | --- |
| 总门禁 | `takeoff_success / transition_success / landed / frames_aligned / stayed_in_area` 全true；非多旋翼还需loiter_airspeed_in_range和rtl_entered；gimbal档需gimbal_command_success；delta额外8项全true，缺证据即失败 | smoke:1063–1087 |
| FW持续飞行 | continuous_fw=true，complete，持续≥120s且绝对绕飞≥360° | smoke:709–717 |
| FW数值质量 | 每类≥30样本；空速p5≥12m/s、p95≤19m/s；abs bank p95≤45°；pitch p5≥−15°、p95≤20°；高度误差p95≤10m | smoke:753–791；delta档案上限18m/s、bank35°加既有容差 |
| 电机饱和 | servo1..4任一≤5或≥1095为饱和；any motor、output和每电机饱和占比都≤0.15 | smoke:46–50、:767–791 |
| FW reposition | ring径向容差8m，连续dwell≥30s、sweep≥360°，遥测空洞不能证明连续驻留 | smoke:40；`src/multidetect/simulator/fixed_wing_reposition.py:13`–`:86` |
| Overspeed | 注入后峰值≥21m/s，事件reached且恢复原参数，恢复用时0..40s，稳定段≥15s、continuous FW、高度误差p95≤10m；稳定尾段饱和占比≤0.15 | smoke:427–461；collector:7000/:7060 |
| 回MC | transition开始到结束+5s窗口，四电机全停的最长连续时间≤1s、最大高度损失≤15m | smoke:250–299、:42–44 |
| 回转换后爬升 | 对应事件complete且climb_m≤20 | smoke:822–826 |
| 相机与范围 | 有相机对齐证据且全部aligned；距起点≤1500m；自遮挡样本≥5 depth frames、遮挡比例<0.05 | smoke:49–51、:1049/:1055、delta self_occlusion判定 |
| 云台 | manager所有权、ID匹配、命令reached且每轴误差≤4°；状态、关节快照与时间戳证据齐备 | smoke:1165–1171；collector:4758；`tests/test_delta_tailsitter_collector_diagnostics.py:548`–`:584` |

保留每次原始MAVLink、Gazebo/px4日志、ULog、图像/depth及snapshot，汇总不得隐藏重试或缺失。现有推广要求还包括同一候选内容的w0000原始10次、21任务各2次、delta fixed/gimbal两个严格门禁（`docs/px4-lockstep-runtime-promotion-20261109.md:5`）。main应建立自己的资格记录，通过后再按现行运行镜像内容契约流程评估推广；CPU单测、源码对比和旧版本receipt不抵扣这些实景门禁。

## 8. 本次文件与验证结果

新增：

- 本报告 `docs/px4-upgrade-analysis-20261115.md`。
- `simulation/px4/airframes-main/4050_gz_delta_tailsitter`、`4051_gz_delta_tailsitter_gimbal`：候选注释与4051模型选择顺序；所有显式参数值保留。
- `tests/test_px4_main_upgrade.py`：候选参数保持、shell语法、wrapper真实shell默认/覆盖执行、main元数据存在、gimbal补丁临时副本应用及上游不变、lockstep关键源码结构、GNSS覆盖名称检查。

没有补丁需要移植：两个目标已上游修复，第三个原样适用，因此没有生成`-main.patch`；原补丁、原airframe、当前Dockerfile、runtime pin和生产harness均未改。

本次单进程CPU运行：

```bash
MULTIDETECT_SIM_LISTEN_HOST=127.0.0.1 \
PX4_MAIN_SOURCE_ROOT=/root/localds/px4up/px4-main PYTHONPATH=src:. \
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest \
  tests/test_delta_tailsitter_assets.py tests/test_px4_gimbal_device_id_patch.py \
  tests/test_px4_runtime_image.py tests/test_px4_main_upgrade.py \
  tests/test_delta_tailsitter_smoke_geometry.py tests/test_delta_tailsitter_signals.py \
  tests/test_delta_tailsitter_collector_diagnostics.py tests/test_gimbal_controller.py \
  tests/test_gimbal_manager_session.py -q -p no:xdist
```

最终结果：共134项，**133通过、1跳过**，耗时6.94秒，无warning（旧gimbal补丁source测试没有设置旧`PX4_SOURCE_ROOT`；本次main专用临时应用测试通过）。初次运行的29条warning来自旧planner测试自动尝试只读`docker network inspect bridge`失败后回退127.0.0.1；没有启动或构建容器。最终按上方命令预设`MULTIDETECT_SIM_LISTEN_HOST=127.0.0.1`重跑，阻止该探测并通过；这不是未来真实容器运行的可达性配置。runtime重建拒绝测试中的docker为临时stub。新增Python文件`ruff check`、`git diff --check`均通过。三个补丁apply-check结果见第2节；未运行PX4 C++编译/并发测试或SITL。

尚未解决的升级阻塞：main专用harness参数/导航证据适配及实际挂载，breaker语义审查，成套runtime打包和依赖版本核对，真实listener/协议、sensor frame/instance、transition重启/quadchute、TECS/autoland和云台角度语义回归。以上均纳入后续步骤，当前不推广。
