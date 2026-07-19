# Multi-Detect 当前目标验收矩阵

本页对应截至 2026-07-16 的 Mode Setting、DET/TRK/LCK/TGT、固定相机、通用检测与真实控制链目标。它只记录可由当前文件、构建和运行结果证明的状态。

## 软件交付状态

| # | 要求 | 当前证据 | 状态 |
|---|---|---|---|
| 1 | 独立 `Mode Setting` | QGC `SelectViewDropdown.qml`、桌面目视启动 | 已通过 |
| 2 | 精简无关设置并隐藏机架切换 | `VehicleConfigView.qml` 屏蔽 PX4 Airframe 页；Mode Setting 只保留任务模式 | 已通过 |
| 3 | 模式 3 仅 LCK/TGT 后显示白色传统准星，并由固定翼姿态控制使目标趋近中心 | `MultiDetectVideoOverlay.qml`；`fixed_wing_aim_control.py` 的真实 MAVLink sender、限幅/失锁/航向保持回归 | 软件与消息级通过，实机舵面待标定 |
| 4 | 触控端滑动、桌面端确认/取消 | `FlyViewCustomLayer.qml` 按 `ScreenTools.isMobile` 分流 | 已通过 |
| 5 | 生产端使用真实链路 | QGC 固定 GR01、RTSP 和签名元数据链；Jetson 已同步真实控制模块并完成在线 smoke/常驻重启 | 联机通过；控制动态待标定 |
| 6 | 人、车、火等候选显示分类颜色 `+` | `MultiDetectVideoOverlay.qml` 候选模型与类别颜色映射 | 已通过 |
| 7 | TRK/LCK 分离，模式 2/3 才允许 LCK | `MultiDetectState.qml` 状态机和协议 ACK | 已通过 |
| 8 | LCK 红框、主目标优先、临近脱锁保持观察 | QGC 红框；`SelectionTargetPool` 和 `FixedCameraObservationEngine` | 软件已通过，动态实测待联机 |
| 9 | 模式 2/3：TRK→LCK→确认→TGT | QGC 状态机、签名协议、自测 | 已通过 |
| 10 | UI 文案精简 | 生产界面使用短状态词；模式 3 只显示视轴误差 | 已通过 |
| 11 | 类别检测优先，任意物体最后回退 | Orin 上火烟、COCO80、VisDrone 三模型错峰；CSRT/KCF/模板回退 | 已通过，部署域车辆/火情数据仍需扩充 |

## 固定相机约束

- 相机没有可动轴。
- 自定义 `FlyViewVideo.qml` 不实例化 `OnScreenGimbalController` 或 `OnScreenCameraTrackingController`。
- 视频非选择态只保留双击全屏，拖动和单击不再发送相机控制输入。
- `FixedCameraObservationEngine` 将主 LCK 图像中心偏差与 V6X roll/pitch/heading 合并；`FixedWingAimController` 仅在 Mode 3 + 主 LCK + 确认 + 飞行门限全部成立时生成有界姿态目标。

## 已验证命令

```powershell
cd C:\Users\TT\Documents\GitHub\Multi-Detect
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m pytest -q
.\scripts\run_goal_acceptance.ps1
```

当前核心测试收集数为 985；QGC 自定义应用回归为 26。QGC 完整运行入口：

`C:\Users\TT\Documents\GitHub\QGroundControl-MultiDetect\build-multidetect-release\staging\bin\MultiDetectGCS.exe`

验收脚本还会校验 QGC 的 Jetson 元数据零配置默认值（`192.168.144.20:14580`、本地 `14581`、strict signing），以及 Windows 用户级两个 operator key 是否已就绪；证据 JSON 只写布尔状态，不写密钥内容。

## 剩余实机门

1. 采集固定三体 RGB 相机的 ChArUco 原始帧，生成通过门禁的内参/安装外参 JSON。
2. 断开动力和物理载荷执行器，验证 roll/pitch 舵面方向、绝对限幅、slew、人工取消和失锁恢复进入前模式。
3. 用真实检测完成 DET→TRK→LCK→TGT、撤销和短时脱锁恢复。
4. 完成长时间运行并记录 FPS、P50/P95 延迟、温度、RSS、重连和队列高水位。

机器可读结果写到 `artifacts/evaluation/multidetect-goal-acceptance-latest.json`。
