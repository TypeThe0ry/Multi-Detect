# 可选云台仿真与记录契约（2026-11-08）

## 结论

`x500_mono_cam` 与 `x500_gimbal` 的 airframe 语义保持分明：固定相机配置不声明 `camera_mount`，无需云台记录；云台机型声明 roll、pitch、yaw 三轴及各自限位/速率。gimbal 采集器现在会在记录 `obs_before` 和 `obs_after` 前 fail closed，要求 RGB 对齐的 Gazebo joint sidecar 有完整有限三轴和四 rotor 状态，至少一个 rotor spin 非零。相关 CPU fixture 和 onboard gate 通过。

静态模型证据与可控 joint-state fixture 证明关节/记录接口可动且轴映射可解释；没有执行 Gazebo/SITL，故不据此宣称物理模拟中的 live 命令响应已验证。follow/track v2 共享目录只找到 onboard 输入摘要，未找到可审计的 episode/步骤或命令记录。

## Airframe 字段矩阵

| Airframe | 配置字段 | 模型关节/传感器 | 观测记录要求 |
| --- | --- | --- | --- |
| `x500_mono_cam` | `camera.mount=downward`；无 `camera_mount` | `simulation/gazebo/models/x500_mono_cam/model.sdf` 合并 `model://x500` 与 `model://mono_cam`，`CameraJoint` 为 fixed。seg overlay 的 `JointStatePublisher` 发布四个 rotor joint，不发布 gimbal joint | 固定几何由 airframe camera profile 表达；不要求 gimbal joint sidecar。propeller joint sidecar 可用时记录，缺失不阻止 fixed-camera observation |
| `x500_gimbal` | `camera.mount=gimbal`；`camera_mount.axes=[roll,pitch,yaw]`，各轴有 limits/rates | `simulation/gazebo/model-overlays/x500_gimbal_seg/model.sdf` 合并 x500 与 gimbal，publisher 列出四 rotor 和 `cgo3_vertical_arm_joint`、`cgo3_horizontal_arm_joint`、`cgo3_camera_joint` | 每个已记录 RGB observation 必须有同 stamp 的 Gazebo 三轴角度/速度和四 rotor 角度/转速；任一缺失或无效即拒绝写入 |

对应配置为 `configs/airframes/x500_mono_cam.json` 和 `configs/airframes/x500_gimbal.json`。两者均为 x500 四旋翼，只有后一机型声明云台字段；没有给 fixed-camera 增加 gimbal 字段。

## 模型与数据来源

- gimbal source model `simulation/gazebo/source-assets/x500_gimbal/gimbal_model.sdf` 定义三个 revolute joint：`cgo3_camera_joint` 对应 pitch，`cgo3_vertical_arm_joint` 对应 yaw，`cgo3_horizontal_arm_joint` 对应 roll。
- 同一 SDF 为三个关节配置 Gazebo `JointPositionController`，命令 topic 分别为 `command/gimbal_pitch`、`command/gimbal_yaw`、`command/gimbal_roll`。gimbal overlay 的 `JointStatePublisher` 发布以上三轴和 `rotor_0_joint` 至 `rotor_3_joint`。
- `scripts/gazebo_sensor_stream.py` 订阅 Gazebo model joint state；`_frame_joint_state` 仅从该样本抽取 joint position/velocity，拒绝未来或超过允许 skew 的样本，并以 RGB frame stamp 标记 frame-bound 状态。gimballed model 启用 `require_complete`，三轴或四 rotor 不全时不会发出该帧。
- `SensorStreamReader` 从深度 companion metadata 取出 sidecar；`build_observation` 将 sidecar 状态写入 observation。新增 `validate_gimbal_recording_observation` 在采集器记录前核对来源、RGB stamp、轴/转子长度与有限性，拒绝 sidecar 缺失时由高层反馈或 ESC RPM 填充出的伪关节状态。
- 旋翼状态直接取 Gazebo joint positions/velocities；未从 actuator output 或后处理常量合成。frame-bound `timestamp_ns` 等于 RGB sensor stamp；源 joint 时间和 skew 由 stream gate 用于因果新鲜度筛选。

## Fixture 与验证

`tests/test_propeller_gimbal_recording.py` 增加了 airframe/SDF 配置 fixture、可控 joint 输入 fixture 和 recording fail-closed fixture。固定相机配置 fixture 验证无 `camera_mount` 且 `build_observation` 可接受没有 gimbal sidecar 的状态。gimbal fixture 验证 publisher 的 3 个 gimbal + 4 个 rotor 名称、三个 position-controller topic，以及输入从 `(pitch,yaw,roll)=(0,0,0)` 改为 `(0.2,-0.3,0.1)` 时 frame gate 记录的三轴变化分别为 `(+0.2,-0.3,+0.1) rad`。四个 rotor 的 fixture 样本均有限，spin 为 `[120,-121,122,-123] rad/s`。

该角度响应 fixture 是受控的 Gazebo joint-state 输入/输出契约测试，结合 SDF 控制器映射证明轴解释与状态传递，不是 Gazebo 物理时间步仿真，也不是非零 PX4/MAVLink 命令响应证据。

fail-closed 反例覆盖缺 gimbal 轴、少于四 rotor、非有限 gimbal angle、RGB/joint timestamp 不一致、全 rotor spin 为零。现有 `tests/test_gazebo_sensor_stream.py` 继续覆盖三轴/四 rotor 缺失、非有限 joint 值和 stale/future sample 拒绝。

执行命令使用主 `.venv`，环境设置为 `PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp`：

```text
python -m pytest tests/test_propeller_gimbal_recording.py tests/test_gazebo_sensor_stream.py tests/test_sim_teacher_live_loop.py -q -p no:xdist
通过；1 skipped
python scripts/run_onboard_gate.py -q -p no:warnings
onboard gate: PASS (123s)
git diff --check
通过
```

没有启动 SITL、Docker、GPU、训练、HIL、真实飞行或模型服务，也没有下载；`artifacts/` 保持只读。测试中生成的 `.tmp/pytest-of-root` 是本地临时缓存。

## follow/track v2 只读复核

检查 `artifacts/benchmarks/follow-track-fixed-gimbal-20261108-v2/follow-track-v2/`：当前仅有五个 mission 目录各自的 `onboard.json`（两个 world 的 fixed-camera/gimbal follow 配对，以及 `follow-track-gimbal-command-audit` postbox inspect 输入）。文件记录慢模型权重路径、任务文字、180 step budget 和 runtime step limit；没有 `report.json`、`episode.json`、`steps.jsonl`、RGB、MAVLink log 或 joint audit 输出。因此无法从当前已落盘文件确认 follow/track slow-model job 是否完成，也没有可用于评价 follow 指标的数据。本轮没有等待该作业或重跑。

`follow-track-gimbal-command-audit/onboard.json` 的任务文字是从四侧检查 postbox，但它只是 onboard 输入摘要，不能证明任务执行、`GIMBAL_DEVICE_SET_ATTITUDE` 曾发送非零四元数，或关节状态响应。v2 的 live SITL 命令响应证据仍未取得：没有可关联的非零命令、Gazebo joint samples、RGB sensor stamps 或 rotor samples。此前报告中的微小角度抖动不作为命令响应证据。

## 修改文件

- `src/multidetect/sim_teacher/streams.py`：新增 gimbal recording observation validator。
- `scripts/collect_sim_teacher_episodes.py`：gimbal `obs_before`/`obs_after` 写入前执行 validator；固定相机分支不变。
- `tests/test_propeller_gimbal_recording.py`：新增 airframe、SDF、受控输入和正反记录契约 fixture。
- `docs/gimbal-simulation-contract-20261108.md`：本报告。

## 建议的下一个任务

1. 在允许 Docker/SITL 的 workstation 上，使用 `/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python` 运行已有 follow/track v2 suite 中的 `follow-track-gimbal-command-audit` 任务一次，确保产生有限且非零的 `GIMBAL_DEVICE_SET_ATTITUDE` 命令并保存 MAVLink、Gazebo joint、RGB stamp、四 rotor 和 episode steps。只写入新的临时/新 bench 目录，不等待慢模型服务状态以外的其他作业，不覆盖共享 `artifacts/` 旧产物；不得修改 expected、follow 阈值、技能序列、frame wait、PX4 timeout、supervisor 安全规则或 fixed-camera airframe 字段。将命令与三轴变化的因果审计写入 `docs/gimbal-simulation-live-command-response-20261108.md`。
2. 在主 `.venv` 中对新增 recording validator 做一次采集器集成回归，使用可控 stream fixture 验证 gimbal frame 被拒绝时 recorder 没有写入 step，fixed-camera 仍可记录无 gimbal 状态；只修改 `src/multidetect/sim_teacher/streams.py`、`scripts/collect_sim_teacher_episodes.py` 和对应测试，不改任务 expected、SITL 阈值/等待/timeout/supervisor 或 `artifacts/`。运行相关 pytest 与 CPU-only onboard gate，写入 `docs/gimbal-recording-collector-fail-closed-20261108.md`。
