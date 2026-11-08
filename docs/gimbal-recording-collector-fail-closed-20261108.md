# 云台采集器拒帧 fail-closed 回归（2026-11-08）

## 结论

采集器的最终 step 持久化入口现在接收当前 `camera_mount`。当机型是 `gimbal` 时，入口在调用 `EpisodeRecorder.record_step` 前再次校验 `obs_before` 和 `obs_after` 的 RGB 时间戳、Gazebo 三轴云台关节状态和四个旋翼关节状态。任一帧不满足契约都会抛出 `ValueError`，因此不会写入 step 行，也不会写入该 step 的 RGB/depth 文件。

固定相机仍走原有路径：`CameraMount.fixed()` 不触发 gimbal contract，即使观测没有 gimbal/propeller sidecar，也可以正常写入 step。该路径保持固定相机的旧行为，不要求人为补造云台状态。

## 实现改动

- `scripts/collect_sim_teacher_episodes.py`
  - `_record_policy_step` 增加 `camera_mount` 参数。
  - gimbal mount 在调用 recorder 前校验 before/after 观测。
  - 运行时 episode 记录调用把当前 `camera_mount` 传入统一入口。
- `tests/test_propeller_gimbal_recording.py`
  - 新增 gimbal 拒帧集成回归：after-frame 的 propeller timestamp 错位时，确认抛出拒绝异常，`recorder.index` 保持 `0`，`steps.jsonl`、RGB 和 depth 均没有新文件。
  - 新增 fixed-camera 集成回归：无 gimbal sidecar 的观测仍写入一行 step，且返回 terminal stop 的继续标志为 `False`。

## 回归证据

执行前设置：

```text
PYTHONPATH=src:.
PYTHONUTF8=1
TMPDIR=$PWD/.tmp
```

相关 pytest：

```text
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python -m pytest \
  tests/test_propeller_gimbal_recording.py \
  tests/test_gazebo_sensor_stream.py \
  tests/test_sim_teacher_live_loop.py \
  -q -p no:xdist
```

结果：`125 passed, 1 skipped`。

CPU-only onboard gate：

```text
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/run_onboard_gate.py -q -p no:warnings
```

结果：`onboard gate: PASS (127s)`。

本次没有启动 SITL、Docker、GPU、训练、HIL、真实飞行或模型服务，没有联网下载，也没有写入 `artifacts/`。`git diff --check` 通过。

## 验证边界

本回归验证的是 collector 到 `EpisodeRecorder` 的写盘顺序和 fixed/gimbal 分支契约。它使用可控 Python fixture，不证明 Gazebo 物理仿真中实时 joint publisher 的调度、PX4 云台命令响应或真实传感器链路时序；这些仍需要允许 SITL/Docker 的独立 live 证据。

## 建议的下一个任务

1. 在允许 Docker/SITL 的 workstation 上，使用 `/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python` 运行已有 follow/track v2 suite 的 `follow-track-gimbal-command-audit` 一次，保存非零 `GIMBAL_DEVICE_SET_ATTITUDE`、Gazebo joint、RGB timestamp、四 rotor 和 episode steps 的可关联证据；只写入新的临时/新 bench 目录，不覆盖 `artifacts/` 旧产物，不修改任务 expected、follow 阈值、frame wait、PX4 timeout 或 supervisor 安全规则；将审计写入 `docs/gimbal-simulation-live-command-response-20261108.md`。

2. 为 collector 增加一个不启动 SITL 的异常路径回归，覆盖 gimbal `obs_before` 拒绝、gimbal `obs_after` 拒绝和 fixed-camera 多步连续记录的 step/file 计数；证据路径为 `tests/test_propeller_gimbal_recording.py`、`scripts/collect_sim_teacher_episodes.py` 和本报告；不得修改任务 expected、SITL 等待/timeout、supervisor 规则、`configs/schemas/*.json` 或 `artifacts/`；运行相关 pytest 与 CPU-only onboard gate，并将结果补写到 `docs/gimbal-recording-collector-fail-closed-20261108.md`。
