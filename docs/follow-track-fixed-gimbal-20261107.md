# follow/track 固定相机与云台覆盖回归

日期：2026-11-07（Asia/Singapore）

本轮建立了一个独立的临时 follow/track suite，覆盖两个不同 generated-movers world，并为每个 world 建立 fixed-camera 与 `x500_gimbal` 一一对应任务。suite 复制了现有 follow 语义和阈值：规划技能必须精确为 `[follow, return_home]`，要求目标在框比例、移动距离、连续 track、时间覆盖、丢失次数、重获时间和 false confirm；没有增加 `area_search`，没有降低阈值，也没有修改任务预算。

临时配置和运行证据：

- suite：`.tmp/campaigns/follow-track-fixed-gimbal-20261107/suite.json`
- dry-run 输出：`.tmp/campaigns/follow-track-fixed-gimbal-20261107/dry-run.jsonl`
- SITL 尝试摘要：`.tmp/campaigns/follow-track-fixed-gimbal-20261107/sitl-attempt.json`
- Docker 可用性证据：`.tmp/campaigns/follow-track-fixed-gimbal-20261107/docker-availability.txt`

## 配置校验

`--dry-run` 使用 `/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python`、`MULTIDETECT_SITL_SLOTS=3`、`--workers 1`，四个任务都规划到 slot 0（端口 `14560/14561`），未访问 slot 3/4。

| 任务 ID | world | airframe | fixed/gimbal 字段 | 规划预期 |
| --- | --- | --- | --- | --- |
| `follow-w0000-fixed-camera` | `gen_mov_w0000` | `x500_mono_cam` | 无 `recording`/gimbal 契约字段 | `follow -> return_home` |
| `follow-w0000-gimbal` | `gen_mov_w0000` | `x500_gimbal` | `recording.per_step = [gimbal_joint_state, propeller_joint_state]` | `follow -> return_home` |
| `follow-w0002-fixed-camera` | `gen_mov_w0002` | `x500_mono_cam` | 无 `recording`/gimbal 契约字段 | `follow -> return_home` |
| `follow-w0002-gimbal` | `gen_mov_w0002` | `x500_gimbal` | `recording.per_step = [gimbal_joint_state, propeller_joint_state]` | `follow -> return_home` |

suite 校验和相关测试证明：fixed-camera 不被强加 gimbal recording；`x500_gimbal` 缺少两个 per-step 字段会被拒绝；Gazebo joint gate 要求 RGB 时间戳对齐、三轴云台和四个 rotor 的角度/转速完整，且 partial/stale actuator sample 会被拒绝。

## SITL 运行结果

本轮实际运行命令为串行 4 mission suite，每个任务 1 episode、180 steps、`max_episode_s=2400`，并保留 PX4 ULog。suite 获得了 slot 0 lease，但在第一个任务的 SITL/Gazebo 初始化阶段没有生成 campaign、episode 或 report；当前执行沙箱直接访问 Docker API 返回 `permission denied`。为避免无限等待，观察到 lock owner 持续更新而产物没有增长后，使用本次 session 的控制句柄终止，退出码为 130，并清理了本次 session 自己留下的 stale lock。

因此四个任务均为基础设施阻塞，不能把未产生的指标当作通过：

| 任务 ID | world | airframe | slot | 首步审计 | 跟随指标 | joint-state 证据 | 失败根因 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `follow-w0000-fixed-camera` | `gen_mov_w0000` | `x500_mono_cam` | 0（计划） | dry-run 预期 `follow`；无 `area_search` | 未取得 | 不适用；fixed-camera 仅校验合法相机字段 | 基础设施：SITL/Docker 初始化未完成 |
| `follow-w0000-gimbal` | `gen_mov_w0000` | `x500_gimbal` | 0（计划） | dry-run 预期 `follow`；无 `area_search` | 未取得 | 未取得 `obs_before/obs_after`；静态契约要求云台三轴+四 rotor | 基础设施：SITL/Docker 初始化未完成 |
| `follow-w0002-fixed-camera` | `gen_mov_w0002` | `x500_mono_cam` | 0（计划） | dry-run 预期 `follow`；无 `area_search` | 未取得 | 不适用；fixed-camera 仅校验合法相机字段 | 基础设施：SITL/Docker 初始化未完成 |
| `follow-w0002-gimbal` | `gen_mov_w0002` | `x500_gimbal` | 0（计划） | dry-run 预期 `follow`；无 `area_search` | 未取得 | 未取得 `obs_before/obs_after`；静态契约要求云台三轴+四 rotor | 基础设施：SITL/Docker 初始化未完成 |

由于没有 episode，以下运行态验收项保持未判定：目标在框比例、移动 track 连续性、丢失/重获、return_home 完成、false confirm、RGB 对齐、云台随控制变化、四 rotor 至少一个非零 spin。这些项目没有用静态或 nightly 结果替代。

## 验证

- 相关 pytest：`179 passed`；覆盖 `tests/test_mission_suite.py`、`tests/test_propeller_gimbal_recording.py`、`tests/test_suite_airframes_resolve.py`、`tests/test_gazebo_sensor_stream.py`、`tests/test_sim_teacher_segmentation_transport.py`。
- CPU-only onboard gate：`onboard gate: PASS (111s)`，含 6 skipped。
- dry-run：4/4 mission 配置生成成功；fixed 2 个任务无 recording 字段；gimbal 2 个任务声明两个 required per-step 字段；所有任务使用 `.venv-vlm/bin/python` 与 slot 0 端口。

本轮没有修改产品代码、expected、技能序列、follow 阈值、任务预算或安全 supervisor 规则。残余覆盖是一次可用 SITL/Docker 主机上的实际重跑；重跑必须复用上述临时 suite，保持 `MULTIDETECT_SITL_SLOTS=3`、`--workers 1`、slot 0–2 限制，并逐 episode 保存原始 `steps.jsonl` 后再补齐本报告的运行态指标。

## 建议的下一个任务

1. 在 Docker API 可用且无其他 SITL 占用的主机上，使用 `.tmp/campaigns/follow-track-fixed-gimbal-20261107/suite.json` 串行重跑 4 个任务各 1 episode，保留 slot、world、provenance、`steps.jsonl` 和失败证据；不得修改 expected、follow 阈值、任务预算、area_search 规则或安全 supervisor；将完整运行态结果补写到 `docs/follow-track-fixed-gimbal-20261107.md`。
2. 对重跑得到的两个 `x500_gimbal` episode 做独立 joint-state 审计，检查每个 `obs_before`/`obs_after` 的 RGB 对齐、pitch/yaw/roll 可解释变化、四 rotor 角度/转速有限且至少一个 spin 非零，并把审计脚本输出及异常分类追加到 `docs/follow-track-fixed-gimbal-20261107.md`；不得放宽 `scripts/gazebo_sensor_stream.py` 或 recording contract。
