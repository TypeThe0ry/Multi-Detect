# 机型档案与机载闭环

机载策略只描述运动意图，例如“去某点”“在目标附近观察”“等待”“返航”和“降落”。`AirframeMotionAdapter` 读取 `configs/airframes/*.json`，把同一意图转换成机型能执行的动作。

## 行为差异

- **多旋翼（默认 `x500_mono_cam`）**：继续使用原来的 `translate`、`turn`、`hold`、`stop` 原语。搜索、接近、确认、返航的逐步动作和输出字段保持原样，可以用原有回归轨迹逐步比对。
- **固定翼**：不能悬停。等待、思考、健康降级和安全拒绝都转换为按 `profile.orbit_radius()` 盘旋；不会发送零速 `hold` 或原地 `turn`。搜索和测绘使用满足最小转弯半径的割草机航线，窄区域自动使用 skip-lane。检查目标使用 observation orbit；确认应使用不同航向的 fly-by，通过地面投影一致性和三角化 sigma 门限确认。`hover` 在计划语义上等价为盘旋，`deliver` 没有经过验证的飞掠投放窗口时直接拒绝。
- **VTOL（`standard_vtol`、`quadtailsitter`、`tiltrotor`）**：巡航、搜索、测绘默认 FW。遥测报告 MC 时，近距观察、等待和降落使用 MC 位置 setpoint；切换模式通过 `vtol_transition`。低能量或安全拒绝先按档案盘旋，再由 supervisor 执行返航/转换/降落策略。
- **相机**：投影使用完整 roll/pitch/yaw。tailsitter 档案可分别提供 `camera.attitude_rotations.mc` 与 `camera.attitude_rotations.fw`，运行时按当前 VTOL 模式选择，避免把 FW 倒置姿态当成 MC 姿态。

## 配置

机载配置增加 `airframe`，可以写档案 id 或 JSON 路径：

```json
{
  "airframe": "standard_vtol"
}
```

缺省值是 `x500_mono_cam`。档案不存在、JSON 字段不合法或能力不一致时，在模型和飞行控制启动前拒绝启动。规划 prompt 和身份 prompt 不会被修改；机型信息只在主机适配层通过档案、能力和遥测传递。

## 接口

`MotionIntent` 是规划/执行层的输入，`MotionCommand` 是执行器边界的可序列化命令。仿真 `FixedWingVtolExecutor.execute_command()` 与真机 `OnboardRuntime` 使用同一命令字段：`reposition`、`orbit`、`transition`、`fw_land`、`mc_reposition`、`mc_hold`。多旋翼仍直接返回原 `ActionPrimitive`，因此现有教师数据和回归轨迹不需要迁移。

## 限制与未验证项

当前固定翼投放只实现安全拒绝；真正的 payload 触发、释放方向和飞掠窗口需要载荷控制器提供协议后再启用。真机高层命令使用 home geodetic 将本地 NED 转成 WGS84，仍需在 PX4 SITL/HIL 和实机上验证原点、磁航向、风场及 ACK 超时。档案中的功率、空速、转弯半径是保守输入，必须用飞行日志重新标定。tailsitter MC/FW 相机姿态已接入纯逻辑投影路径，仍需用两种实际姿态的标定图像验证地面投影误差。

## 仿真闭环（2026-10-18）

采集器 `--teacher onboard --airframe <机型>` 现在会把机型交给机上策略：非多旋翼机型由策略输出 `MotionCommand`，交给 `FixedWingVtolExecutor` 执行；重定位要等飞到目标附近（或按巡航速度算出的超时），定时盘旋要等满时长，避免每一帧都换一个指令。步骤记录保留原始动作，实际飞的命令写在 `labels["motion"]`，实际位移从遥测计算。

```powershell
python scripts/collect_sim_teacher_episodes.py --airframe rc_cessna_mono_cam `
  --scenario configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json `
  --teacher onboard --episodes 1 --steps-per-episode 30 --out .tmp/closed-loop-cessna `
  --container-prefix smoke-cl --control-port 14588 --telemetry-port 14589
```

真实 SITL 结果（rc_cessna，海岸巡逻场景）：30 步全部由机上 AI 决策，30 条命令 PX4 全部接受执行，之后返航（确认进入 RTL）并在 home 跑道降落，全程不读真值。途中修了一处：命令 ACK 的等待时间被写成 0，导致 PX4 已接受的命令全被记成失败。

**还没解决的**：机上搜索目前是按多旋翼的尺度设计的——每步最多 15 m，很多步是“原地等待”。固定翼不能悬停，最小转弯半径约 21 m，于是只是在附近绕圈、缓慢漂移（30 步只移动约 140 m），没有形成真正的搜索。下一步要按机型给搜索参数：长航段的割草机航线、按转弯半径安排掉头、用不同航向的飞掠来确认目标。
