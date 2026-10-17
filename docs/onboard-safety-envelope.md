# 机载 Safety Envelope（最终动作仲裁器）

Safety Envelope 是自主飞行链路的最后一道动作边界。策略、任务执行器、MissionSupervisor、恢复动作和健康门先做决定；所有输出的 `ActionPrimitive` 在写入 20 Hz `SetpointPump` 前经过同一个仲裁器。SIM 的 `OnboardPolicy.act` 也在唯一返回点调用同一仲裁器，因此 supervisor/health 的早返回不会绕过检查，也不会在一帧重复调用 `next_action`。

## 责任边界

仲裁器只检查、裁剪或保持动作，并记录原因。它不会直接修改 executor 或 supervisor 状态。拒绝通过 `MissionSupervisor.report_envelope_violation(reason, action=...)` 报告，由 supervisor 按既有优先级决定 `return_now` 或 `land_now`；已锁存的 `land_now` 始终优先。报告首次立即升级，重复的同一 `(reason, requested_action)` 事件最多每 5 秒记录一次。

机载 runtime 只有一个 `_set_target_checked(target, purpose)` 写入边界。`policy_action`、`takeoff`、`hazard_hold`、`policy_hold`、`close_hold` 和 `terminal_hold` 都经过该 helper；代码中没有其它 `SetpointPump.set_target` 调用。hold 永远写入当前目标，即使当前位置已经越界，同时把当前位置越界报告给 supervisor。takeoff 需要已知 home，目标高度会夹到 `max_altitude_m`。

策略返回的标签带有 `safety` 时，说明该 primitive 已由策略级 Envelope 处理。runtime 只对最终绝对 setpoint 做轻量检查，不再次以不同规则裁剪，避免 double clipping；未带标签的通用 policy 结果才在 runtime 边界执行 altitude 检查。

## 几何与高度语义

水平步长限制只作用于 North/East 分量；forward+down 混合动作不会按 3-D 范数缩短。垂直分量超过 `max_vertical_step_m` 时，本帧夹到该上限，下一帧由 policy/executor 重新发剩余动作。runtime 不维护 vertical segment queue，因此 supervisor、health 和 timeout 每帧都能重新生效。

`min_altitude_m`、`max_altitude_m` 是相对任务 home 的高度（local NED 的 down 轴），home 来自任务 package/executor，绝不从第一帧 pose 推断。默认 `min_altitude_m=0`，保持 integration 对现有合法动作的字节级行为。`min_agl_m` 只有在 ground model 或 AGL telemetry 可用时才检查；没有可靠 AGL 时仅应用 home-relative 限制。

默认 `low_altitude_exception_m=0`。仅在 `deliver` 或 `land/landing` skill 的 descent/release/landing phase 生效，把该值作为受控最低高度；例如配置为 2 m 时，deliver descent 可以到 2 m，但普通搜索动作仍遵守 `min_altitude_m`。

Operating area、no-fly polygon、home radius 和 obstacle 检查作用于完整动作线段。障碍物通过 `ObstacleSource`（可组合静态地图和 depth source）提供，不接受散落 callback；home radius 使用 `effective_radius_from_home()`。

## 三种机型

`VehicleDynamics` 是包络的轻量输入，主线程可以把 `AirframeProfile` 转成它；不传时默认就是多旋翼。

- 多旋翼继续使用原来的高度、水平步长、home 半径、直线段障碍检查，拒绝动作仍是 `hold`，现有动作对象和值不变。
- 固定翼要求空速位于失速余量到最大空速之间，并检查最大坡度、最小转弯半径和爬升/下降率。转弯不再用直线弦判断，包络会沿圆弧采样每个短段；命中围栏或障碍时拒绝，并在决策元数据中给出 `loiter` 作为安全动作。
- VTOL 根据 `mode` 在 `mc`、`fw` 和 `transition` 间切换。`transition` 使用更严格的联合包络，要求转换高度并把允许坡度收紧；模式或关键空速未知时按 fail-closed 处理。

底层 `ActionPrimitive` 仍只有 `translate/turn/hold/stop`，所以 `loiter`、`vtol_transition_to_mc`、`fw_land`、`parachute` 作为 supervisor/health 的恢复动作枚举和序列输出，不伪装成不兼容的 primitive。

## 验证

定向测试覆盖混合 forward/down、跨帧 25 m climb、home-relative 与 AGL、package home、deliver 2 m 例外、supervisor land 优先级、所有早返回路径、operating/no-fly 和默认配置字节兼容性。运行：

```powershell
$env:CUDA_VISIBLE_DEVICES='-1'
$env:TEMP=(Join-Path (Get-Location) '.testrun')
$env:TMP=$env:TEMP; $env:TMPDIR=$env:TEMP
python scripts/run_onboard_gate.py -q -p no:warnings
```
