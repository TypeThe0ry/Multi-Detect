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

## 2026-11-08 产物复核

本节针对只读产物 `artifacts/benchmarks/follow-track-fixed-gimbal-20261108/` 做离线复核。四个 mission 都有 1 个 episode，均为 `status=complete`、2 步、`mission_completed=false`、`confirmed=false`、`success=false`、`safety_pass=true`。第 1 步 onboard 为 `health_hold`（原因 `slow_unavailable`），第 2 步为 `mission=done` 并执行 `return_home` recovery；因此报告中的跟随字段为 `null`，表示没有进入可评分的跟随阶段，不应解释为 0 次丢失或 0 秒重获。

套件 expected 保持原值：技能精确为 `[follow, return_home]`；门槛为在框率 `>=0.8`、移动平均/最大距离 `<=10/20 m`、track 连续性和时间覆盖 `>=0.8`、丢失 `<=3`、最大重获 `<=5 s`、false confirm `<=0`。四项的 `expectations_passed` 均为 `false`，原因是计划技能未报告且所有 follow 指标均为 `null`。episode 内的 `mission_summary.json` 记录了 `area_search -> follow -> return_home`，但三个步骤均为 `unknown/no_recorded_completion`，所以不能据此宣称 expected 通过。

| 配对 | mission outcome | expected pass | 跟随指标（在框率；均距/最大距；丢失；重获；误跟） | 安全摘要 |
| --- | --- | --- | --- | --- |
| `gen_mov_w0000` fixed-camera | 2 步后 health hold；未确认、未完成 | 否 | `—；—/—；—；—；—` | 最低相对高度 4.476 m；return-home 距离 0.029 m；安全检查通过 |
| `gen_mov_w0000` `x500_gimbal` | 2 步后 health hold；未确认、未完成 | 否 | `—；—/—；—；—；—` | 最低相对高度 4.409 m；return-home 距离 0.028 m；安全检查通过 |
| `gen_mov_w0002` fixed-camera | 2 步后 health hold；未确认、未完成 | 否 | `—；—/—；—；—；—` | 最低相对高度 4.467 m；return-home 距离 0.042 m；安全检查通过 |
| `gen_mov_w0002` `x500_gimbal` | 2 步后 health hold；未确认、未完成 | 否 | `—；—/—；—；—；—` | 最低相对高度 4.512 m；return-home 距离 0.014 m；安全检查通过 |

固定相机与云台在两个 world 上的结果相同：都只执行了 hold/stop，未进入 follow；云台相对固定相机的差异只出现在传感器记录层。`fixed-camera` 没有 `gimbal_joint_state`，符合其 recording contract；两类 episode 都记录了四个 rotor 的状态。

### 两个 `x500_gimbal` episode 的逐观测审计

审计对象是每个 episode 的 4 个观测（step 0/1 的 `obs_before` 和 `obs_after`），共 8 个。每个观测的 `frame_sensor_stamp_ns`、`camera_info.camera_info_stamp_ns`、`segmentation_info.rgb_sensor_stamp_ns`、`segmentation_info.source_stamp_ns`、`gimbal_joint_state.timestamp_ns` 和 `propeller_joint_state.timestamp_ns` 完全相等，RGB 文件均存在且为 1280×960，`gimbal_joint_state.fresh=true`。四个 rotor 的 `angles_rad`/`spin_rad_s` 均为长度 4 且有限；8 个观测中的四个 spin 均非零。

| episode | 观测 | RGB/关节时间戳（ns） | pitch/roll/yaw 角度变化（`after-before`，rad） | 审计结果 |
| --- | --- | ---: | --- | --- |
| `follow-w0000-gimbal` | step 0 before/after | 30164000000 / 31320000000 | `+1.0666e-05 / -6.3503e-05 / +3.3163e-05` | 对齐；四 rotor；四 spin 非零 |
| `follow-w0000-gimbal` | step 1 before/after | 31484000000 / 31616000000 | `-5.7465e-06 / +2.0537e-05 / -1.1856e-05` | 对齐；四 rotor；四 spin 非零 |
| `follow-w0002-gimbal` | step 0 before/after | 27788000000 / 28944000000 | `+3.2898e-05 / -6.3129e-05 / -1.35796e-04` | 对齐；四 rotor；四 spin 非零 |
| `follow-w0002-gimbal` | step 1 before/after | 29108000000 / 29208000000 | `+1.9989e-06 / +4.6274e-06 / +2.9498e-05` | 对齐；四 rotor；四 spin 非零 |

上述角度变化是观测到的微小变化，不能作为“随命令转动”的通过证据。两个 MAVLink 日志分别有 166 和 157 条 `GIMBAL_DEVICE_SET_ATTITUDE`；其中有限四元数命令分别为 61 和 62 条，换算后的 roll/pitch/yaw 均为 `0/0/0`，其余命令字段含 NaN，且 episode action 只有 `hold` 与 `stop`、没有非零云台轴命令。因此本轮结论是：

- RGB stamp 对齐：8/8 通过。
- 三轴字段完整且每个 before/after 都发生了非零数值变化：8/8 记录到，但变化未与非零云台命令建立因果关联，命令响应审计为未证明。
- 四 rotor 角度/转速完整、有限且每个观测至少一个 spin 非零：8/8 通过（实际四个 spin 都非零）。

本轮没有发现需要修改的明确产品缺陷：follow 为空是运行提前停在 `slow_unavailable` 的结果，云台轴变化也没有对应非零控制输入。没有放宽 `scripts/gazebo_sensor_stream.py`、recording contract、expected 或任何 follow 阈值，也没有写入 `artifacts/`。

## 建议的下一个任务

1. 在 slow model 可用且 Docker/SITL 资源稳定的主机上，复用 `artifacts/benchmarks/follow-track-fixed-gimbal-20261108/` 对应的四 mission 配置各重跑 1 episode，确保至少进入 `follow` 并生成非空 follow metrics；保留 `steps.jsonl`、report、slot 和 provenance，不修改 expected、follow 阈值、任务预算、`scripts/gazebo_sensor_stream.py` 或 recording contract；将结果追加到 `docs/follow-track-fixed-gimbal-20261107.md`。
2. 在一次包含非零云台姿态命令的 `x500_gimbal` episode 上，按 `scripts/analyze_mavlink_postflight.py` 与每个 `obs_before/obs_after` 联合审计命令四元数、三轴角度、RGB stamp 和四 rotor 状态，区分真实命令响应与静态抖动；不写入既有 `artifacts/`，不改变采集脚本或阈值；将审计结论写入 `docs/follow-track-fixed-gimbal-20261107.md`。

## 2026-11-08 v2 重跑计划

针对上述 `slow_unavailable` 结果，新增单槽 nightly 重跑 job：`.tmp/jobs/01-follow-track-v2.sh`。job 选择 `--model-set r21idr3-r24plan`，通过 nightly harness 的 `--custom-suite NAME=PATH` 使用运行时生成的 suite，并固定 `.venv-vlm/bin/python`、GPU0、`MULTIDETECT_SITL_SLOTS=1`、`--workers 1`。suite 从该 model-set 的 follow 配置继承慢模型路径以及原始 expected、180 step 预算和 2400 秒 episode 上限；将同一任务映射到两个 world 的 fixed-camera 与 `x500_gimbal`，共四项。第五项复用 nightly gimbal suite 的 postbox inspect 任务，`inspect` 的目标瞄准会请求非零俯仰/偏航姿态，逐步保留 `gimbal_joint_state` 和 `propeller_joint_state`，用于命令响应审计。

新输出目标为 `artifacts/benchmarks/follow-track-fixed-gimbal-20261108-v2/`，本轮只写入 job 和文档，没有启动 SITL，也没有向只读 `artifacts/` 写入任何数据。执行后应先确认第五项的 `steps.jsonl` 中实际发出了有限且非零的 `GIMBAL_DEVICE_SET_ATTITUDE` 四元数，再联合 MAVLink 与 `obs_before/obs_after` 审计 RGB 时间戳、云台三轴和四 rotor；若 mission 没有进入 inspect，则命令响应结论仍为未取得。不得修改 expected、阈值、预算或 recording contract。
