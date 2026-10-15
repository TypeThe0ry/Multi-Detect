# 机载 Safety Envelope（最终动作仲裁器）

Safety Envelope 是自主飞行链路的最后一道动作边界。策略、任务执行器、MissionSupervisor、恢复动作和健康门先做决定，随后所有 `ActionPrimitive` 统一进入一个仲裁器，仲裁结果才允许写入 20 Hz `SetpointPump`。SIM 的 `OnboardPolicy.act` 也在唯一返回点使用同一个仲裁器，避免某条早返回路径绕过检查。

## 边界和责任

仲裁器只负责检查、裁剪、分段或保持动作，并记录原因；它不会直接修改 MissionExecutor 的状态。发生拒绝时通过 `MissionSupervisor.report_envelope_violation(...)` 报告，由 supervisor 按既有优先级决定 `return_now` 或 `land_now`（已锁存的 `land_now` 始终优先）。运行时只接收仲裁后的 primitive，因此不会出现一帧调用两次 `next_action` 的情况。

在 REAL/HIL，`OnboardRuntime(..., arbiter=...)` 在 worker 结果被 coordinator 消费、设置 pump target 之前调用 arbiter。未传入 arbiter 时参数默认为 `None`，保持 integration 分支的动作字节级兼容。仲裁器可实现 `arbitrate(action, pose_ned=..., yaw_deg=..., observation=...)`，也可提供等价的 `apply` 或 callable；返回 `ActionPrimitive`，或 `(ActionPrimitive, info)`。结果会附加到 `autonomy_action` 事件中。

## 几何和高度语义

水平步长限制只作用于 North/East 分量。包含前进和下降的混合 primitive 不会被 3-D 范数重新缩短；超过限制的纯垂直动作由 envelope 返回多个不超过 `max_vertical_step_m` 的片段，调用方按顺序消费。`min_altitude_m` 与 `max_altitude_m` 以 home 为原点的 local NED 高度解释，home 来自任务 package/executor，绝不把第一帧 pose 当作 home。可用 ground model/AGL telemetry 时，`min_agl_m` 额外检查 AGL；没有可靠 AGL 时仅应用 home-relative 限制，并在审计记录中说明。

默认 `min_altitude_m=0`，其余 envelope 限制关闭或沿用 integration 的 15 m 水平步长，因此当前合法动作的输出保持不变。deliver 的下降和 landing 阶段可以显式启用低高度例外；例外只在对应 skill phase 生效，普通搜索/巡航动作仍受最小高度约束。

## 空域、障碍和恢复

Operating area 多边形、no-fly 区域以及 home radius 在动作完整线段上检查，使用统一的有效半径 helper。障碍检测依赖 `ObstacleSource` 接口，可组合静态地图和可选 depth source；仲裁器不接受散落的 callback，避免不同策略使用不一致的障碍语义。

连续拒绝计数只用于生成审计信息和向 supervisor 报告。仲裁器本身只返回 hold/clip/分段结果，不发送 RTL/LAND，也不写 executor 状态；terminal handoff 仍由 MissionSupervisor 与 OnboardRuntime 的既有 precedence/确认流程完成。

## 验证要求

测试覆盖混合 forward+down 动作、25 m 爬升分段及完成、home-relative 与 AGL 高度、package home、deliver 2 m 下降例外、supervisor 的 land 优先级、所有早返回路径、operating-area/no-fly 线段，以及默认配置下 envelope 开关前后的动作字节相等。运行 targeted onboard tests 后，再执行：

```powershell
$env:CUDA_VISIBLE_DEVICES='-1'
$env:TEMP=(Join-Path (Get-Location) 'tmp'); $env:TMP=$env:TEMP; $env:TMPDIR=$env:TEMP
python scripts/run_onboard_gate.py -q -p no:warnings
```

