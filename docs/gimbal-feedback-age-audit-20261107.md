# gimbal 高层反馈年龄与 joint sidecar 一致性审计（2026-11-07）

## 结论

29-head gimbal 产物中的 32 个高层 `gimbal_state.fresh=false` 样本都同时有新鲜的 Gazebo joint sidecar；没有发现 joint 缺失、joint 过期、三轴非有限或 rotor 格式错误。stale 样本不应提供当前云台相机姿态，且不能回退成 fixed-camera 射线。现有任务语义结果保持不变：七个 gimbal episode 的 `completion_rate=1.0`、`expectation_pass_rate=1.0`，survey 的 coverage、monitor/pause-resume 的告警断言和 count 的 exact 断言没有放宽。

审计修复了两个边界：观测记录现在把 Gazebo joint sidecar 作为独立测量完整替换高层派生的 joint 字段；任务 scorer 在发现 stale 高层反馈时优先拒绝任何保留的 composed pose，避免旧记录重新启用 stale 角度。控制器的职责保持为发送有界的 gimbal 命令并记录命令；它不把反馈缓存当作当前姿态来驱动命令。

## 样本与对齐

审计脚本和中间表位于 `.tmp/gimbal-feedback-age-audit/`：

- `alignment.csv`：每个 `obs_before`/`obs_after` 的 RGB sensor stamp、高层反馈年龄/新鲜度、joint stamp/新鲜度、三轴与四 rotor 数值、动作和 gimbal 命令。
- `summary.json`：组合计数和完整性结果。
- `report-count-summary-diff.json`：旧 count summary 与按任务技能过滤后的离线差异。

可读的七个 `steps.jsonl` 实际合计为 `484` steps、`968` before/after joint samples；`docs/nightly-review-29-head-20261107.md` 写的是 `491` steps、`982` samples，差异为 `7` steps、`14` samples。审计没有修改 artifacts；报告中的 32 个 stale 样本计数与文档一致，以下按实际可读文件报告：

| 任务 | 实际 steps | joint samples | 高层 stale | fresh 高层 + fresh joint | stale 高层 + fresh joint |
| --- | ---: | ---: | ---: | ---: | ---: |
| `gimbal-count-cones` | 89 | 178 | 1 | 177 | 1 |
| `gimbal-count-fire-hydrants` | 108 | 216 | 1 | 215 | 1 |
| `gimbal-find-suv-land` | 23 | 46 | 1 | 45 | 1 |
| `gimbal-inspect-postbox` | 24 | 48 | 1 | 47 | 1 |
| `gimbal-monitor-backpacks` | 107 | 214 | 2 | 212 | 2 |
| `gimbal-operator-pause-resume` | 85 | 170 | 7 | 163 | 7 |
| `gimbal-survey-site-overlap` | 48 | 96 | 19 | 77 | 19 |
| **合计** | **484** | **968** | **32** | **936** | **32** |

所有样本的 joint 三轴位置/速度均有限；propeller `joint_names` 始终为四个 `rotor_0_joint` 到 `rotor_3_joint`，角度和转速长度均为 4 且有限，`spin_rad_s` 没有全零样本。456 个 step 有记录的 gimbal command，484 个 step 的 before/after 三轴 joint 角度有非零差异；这证明命令和物理状态可审计，但不单独宣称每次差异由某一个命令造成。

## 四类行为契约

| 对齐组合 | 控制器 | 观测/记录 | 任务 scorer |
| --- | --- | --- | --- |
| 高层 fresh、joint fresh、RGB stamp 对齐 | 发送限幅后的 pitch/yaw 命令；反馈只作观测 | 记录 `gimbal_state.fresh=true`、`camera_pose`、fresh joint sidecar 和 rotor 四元组 | 可使用 composed pose 做投影和任务评分 |
| 高层 stale、joint fresh、RGB stamp 对齐 | 不把 stale feedback 当作当前控制姿态；命令仍按 mount limits 发送并留在 step labels | 保留 stale `gimbal_state` 供诊断；joint sidecar 完整记录实际三轴/四 rotor；不生成 `camera_pose` | stale 标志优先，投影返回无效；不得使用 stale 角度，也不得伪造 fixed-camera |
| joint 过期或缺失（`x500_gimbal`） | 不把缺失 joint 当作可用姿态 | Gazebo frame gate 拒绝该 RGB 帧；不会进入 recorder/scorer | 没有可评分帧，任务断言不会因缺字段被放宽 |
| 合法 fixed-camera、无 gimbal | 固定相机控制路径不需要 gimbal feedback | `gimbal_joint_state.source=fixed_mount`，缺 gimbal sidecar 合法；propeller sidecar 仍按固定/旧机型可选 | 保持 fixed-camera 投影和原有任务语义，不要求 gimbal 字段 |

## 代码与 fixture

- `src/multidetect/sim_teacher/streams.py`：sidecar 存在时直接采用 sidecar mapping，并只规范 `timestamp_ns`；不再与 stale MAVLink 派生字段合并。
- `src/multidetect/simulator/camera_projection.py`：先检查 `gimbal_state.fresh=false`，再检查 `camera_pose`，所以遗留的 composed pose 不能覆盖 stale 判定。
- `tests/test_sim_teacher_live_loop.py`：stale 高层角度与 fresh joint 角度不同的 fixture，验证 sidecar 角度和 rotor 记录不被覆盖、无 `camera_pose`。
- `tests/test_camera_mount.py`：验证 stale 高层反馈优先于保留的 composed pose；原有 stale ground-ray/count 回归继续通过。
- `tests/test_gazebo_sensor_stream.py`：验证非有限 rotor 样本被完整 gimbal frame gate 拒绝；已有缺 rotor/缺 gimbal、四 rotor 长度和 spin fixture继续覆盖。
- `scripts/eval_onboard_loop_campaign.py` 的 `_count_summary()` 已按 `mission_plan_skills` 中是否含 `count` 过滤；本次核对该统计边界并用现有回归测试锁定，没有改变任务通过语义。

## 旧报告离线差异

对七个 gimbal `report.json` 的 episode rows 重算：旧逻辑会把 survey、monitor 和 pause/resume 的 `expected_count=3` 也纳入 count summary，共 5 行，得到 `count_exact_rate=0.4`、`mean_abs_count_error=1.8`。按 mission plan skill 过滤后只剩两个真正的 count episode，得到 `count_exact_rate=1.0`、`mean_abs_count_error=0.0`。suite 的 `mission_completed`、`survey_coverage`、告警 precision/recall、`expectation_checks` 和 `expectations_passed` 均未改写；因此这是 reporting 统计差异，不是放宽任务判定。

32 个 stale 样本中 survey 占 19 个、pause/resume 占 7 个。所有 stale 样本都没有 `camera_pose`，且对应 joint fresh；因此它们没有被当作当前云台角度参与新的投影。已有 29-head suite report 仍显示七项完成、计划和期望检查通过，不能据此把 stale 样本解释成任务失败，也不能把任务通过解释成 stale pose 合法。

## 验证与残余风险

执行环境为主 `.venv`，未启动 SITL、Docker、GPU、训练、HIL 或真实飞行；`artifacts/` 只读。相关测试结果：

```text
143 passed, 1 skipped in 8.63s
onboard gate: PASS (122s)
```

残余风险是产物正文的 `491/982` 与七个可读 `steps.jsonl` 的 `484/968` 计数差异尚未追溯到缺失的 7 个 step；本次没有用缺失样本推断额外 stale 组合。另未重新运行 gimbal 套件，因此不能新增声明控制器在 live SITL 中的时延或 PX4 ACK 行为；本审计只证明现有记录、CPU fixture 和 scorer fail-closed 语义。

## 建议的下一个任务

1. 在允许 SITL 的 workstation 上补齐 29-head gimbal 计数差异：定位文档声称但当前共享目录缺失的 7 个 steps/14 个 before-after 样本，保留原始 episode/provenance，只写新的 `.tmp` 审计副本和 `docs/gimbal-29head-step-count-reconciliation-20261107.md`；不要修改既有 artifacts、任务 expected、技能序列、count/survey/alert 阈值或本报告的 freshness 语义。
2. 运行一个固定相机与一个 `x500_gimbal` 的同语义 survey 或 monitor SITL 对照，记录高层 feedback age、joint sidecar、RGB stamp、gimbal command、pause/resume 或 alert 事件和任务断言；只修 simulator/observation/recording 的最小缺陷，输出到 `docs/gimbal-fixed-vs-gimbal-freshness-followup-20261107.md`，不要改普通 fixed-camera 配置或放宽任何任务通过规则。
