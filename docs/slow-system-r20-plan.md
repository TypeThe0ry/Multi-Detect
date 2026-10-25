# 慢模型 r20 难例方案

## 目标

r20 只做离线方案、世界生成和小规模 SFT 样例，不采集 SITL、不训练。第一门禁是保持真实飞行集 v1 的干扰物拒绝率不低于 r18（0.974），并且不能重复 r19 的 0.881 回退；身份正例召回和规划意图检查作为第二门禁。

## 错误类型与样本

| 错误切片 | 正样本 | 必须配对的负样本 |
|---|---|---|
| pickup/SUV 身份混淆 | pickup 清晰框、不同颜色/车头车尾 | 同一帧 SUV 框，任务仍为 pickup；反向 SUV/pickup 也成对 |
| 交通锥/jersey barrier | 锥桶正类，遮挡和不同朝向 | 同一世界 barrier 框拒绝，不能只用背景负样本 |
| 固定翼高空救护车等 | ambulance、bus、SUV、fire truck 的 20--40 px 正类 | 同视角、同斜距的其它车辆拒绝；车顶、车尾、45 度各至少一组 |
| 背包小斜视 | 10--13 px、18--20 m 高、25--35 m 斜距的背包 | cardboard box/消防栓同尺寸拒绝；同一候选框反事实拒绝 |
| 消防栓截断/东北视角 | h2 屋顶上方东北视角与下沿截断正类 | 截断背景和邻近设施拒绝，保留 visible fraction 元数据 |
| 返航意图 | “到 X 做事然后返航/回 home/回起飞点” -> `goto` 后 `return_home` | 同文本 `goto -> land` 的纠正/拒绝样本 |
| 未知地点 | 任务提到 Raven Pier 等未知地点 | 不能把已知 gate/pad 代替未知地点；`ungrounded_place` 必须命中 |

正负比例建议身份数据 1:1.5（其中至少 70% 是同世界、同视角配对拒绝），小框层 16--20 px 不低于总身份数据 25%，固定翼视角不低于 20%。规划 r20 additions 由正确/错误各半组成，错误样本不进入正例配对统计。

观察几何与出生状态必须分开：`vehicle.spawn_pose` 表示地面上的物理初始状态，不得承载相机观察高度或观察俯仰。多旋翼从 `z_up_m=0.2`、`pitch_rad=0` 起飞；固定翼按其 airframe profile 的跑道/手抛/弹射配方出生，并在场景与采集参数中显式指定 profile 和起飞方式。相机高度、俯仰、巡航高度及观察航点属于任务/teacher 意图，必须由 teacher/采集器消费。

### runtime 契约修复

这次失败的根因是生成器把 `task_intent` 写入 `*.runtime_input.json`。`ScenarioRuntimeInput` 是仿真运行时的严格契约，未知字段会在 stack planner 入口直接拒绝；把 teacher 几何加进该 JSON 还会改变 runtime channel 的 provenance 哈希。采用方案 (a)：生成器把观察意图写到同目录、同 world id 的 `*.teacher_intent.json` sidecar。它不属于 scenario manifest 的三条受证据约束 channel，也不进入 runtime hash；collector 的 `expert_search` 只从该 sidecar 读取，缺失时保持旧的默认策略。没有修改 `configs/schemas/*.json`，后续若要把 sidecar 正式纳入清单，交给生成器/schema 流程处理。

`run_r15_round.py collect` 现在在派发 worker 前，用每个场景实际的 `--airframe` 和 fixed-wing `--takeoff-method` 构造 collector 的 dry plan，调用同一 `docker_command`/stack planner。新增离线端到端测试覆盖 generated-r20（20 个）和 generated-r21id（24 个）全部场景，不启动 Docker。

## 世界与分布

`scripts/generate_r15_worlds.py --profile r20` 是隔离开关，默认行为不变。正式集合使用仓库内资产，并按四类模板分层生成：成对干扰物、fixed-wing 高空斜视、背包远小斜视、建筑物遮挡消防栓/画面下沿截断。真目标坐标同步写入 SDF 与 truth，vehicle runtime 按场景配置高度/姿态。所有目标组合必须通过 held-out 检查。

补采/后续扩展必须继承 `configs/scenarios/generated-v2/index.json` 的 `held_out_target_combos`，不得把 test 组合回灌 train。先按世界、目标类、候选类和尺寸分层，再合并；每个反事实正例都要有同帧拒绝。

## 门禁与验收

离线阶段检查：所有世界 truth 与 index 可解析；每个 paired world 至少一个正类和一个标注 confuser；r20 规划正确样本 `check_plan_intent` 违规数为 0，错误样本分别命中 `return_intent_land` 和 `ungrounded_place`。单元测试覆盖 profile 参数、配对字段、默认兼容性和 CLI 开关。

候选模型验收必须同时满足：v1 干扰物拒绝率 >= r18 0.974；pickup/SUV 与 cone/barrier 配对拒绝率 >= 0.97；16--20 px 拒绝率 >= r18 对应切片；固定翼车辆拒绝率不低于 v1 基线；规划返航和未知地点拒绝 100%。任一身份拒绝门禁下降即否决 r20，即使小目标召回提升也不替换 r18。

## r20 世界资产生成与本地验证（2026-10-24，后续修订）

按固定 seed `20261024` 运行已合并的 `scripts/generate_r15_worlds.py --profile r20 --count 20 --world-prefix gen_r20_w`，资产落在 `configs/scenarios/generated-r20/`、`simulation/gazebo/worlds/`、`simulation/maps/`、`simulation/terrain/`。为兼容 `run_r15_round.py collect` 固定要求的 20 世界，索引划分为 16 train / 4 val / 0 test；`held_out_target_combos` 原样继承 `generated-v2/index.json`。

修订生成器后，20 世界分布为：

| 场景类别 | 世界数 | 真目标→成对干扰物 |
|---|---:|---|
| 干扰物成对 | 6 | construction_cone→jersey_barrier（4）；hatchback_blue→ambulance（1）；ambulance→bus（1） |
| 固定翼高空斜视 | 5 | fire_truck→ambulance（5） |
| 背包远小斜视 | 5 | backpack→cardboard_box（5） |
| 消防栓建筑遮挡/下沿截断 | 4 | fire_hydrant→cardboard_box（4） |

所有真目标与 v2 held-out 组合无交集。r19 的两项回退中，pickup/SUV 的 SUV 和 pickup 均在 `generated-v2/index.json` 的 held-out 列表，不能直接作为 r20 真目标；因此以 catalog 中符合 target/distractor 资格且不在留出表的 hatchback_blue→ambulance 外观相近车辆对替代。锥桶/jersey barrier 不属于 held-out，故成对组以 construction_cone→jersey_barrier 为主：4 个世界同框近距配对，名义锥桶短边尺寸层 16–20 px；另保留 1 个 ambulance→bus。固定翼 5 个世界显式使用可解析的 `rc_cessna_runway` 与 runway 起飞，runtime 机型为固定翼标准模型，并使用该模型对应的 RGB/depth/segmentation assets。背包组的约 19 m 相机高度、约 28 m 地面距离及消防栓组的高位下俯观察均记录为任务/teacher 几何意图；出生仍按机型地面起飞配方配置。上述为配置几何验收，SITL 冒烟留待排队执行。

逐世界 stack plan 预检通过，SDF 可解析且三路 channel hashes 与内容一致。此前版本在旧提交 `49bb467e` 的对照 worktree 中对 r20 20/20 场景全部复现 `ScenarioRuntimeInput has unknown field(s): task_intent`；修复后离线 collector plan 测试覆盖 r20 20/20 与 r21id 24/24。重新生成只把 teacher 意图从 runtime sidecar 移出，world id、seed、布局、划分和 holdout 保持不变；可用 `git diff --no-index` 对照生成前后对应 world/SDF/map/DEM，确认没有几何资产漂移。

本次修复根因：旧生成器把 18–75 m 的相机观察高度和负俯仰直接写进多旋翼 spawn，PX4 因空中/倾斜出生拒绝解锁；此外 5 个固定翼场景未向 collector 传入固定翼 airframe，导致默认 `x500_mono_cam` 与 runtime model 不匹配。collect 现在应在派发 worker 前汇总并拒绝 runtime model/airframe 不一致、不可解析 airframe 及不贴地的多旋翼出生配置，确定性配置错误不进入重试；瞬时故障仍使用原重试次数。

由排队操作者执行的 SITL 冒烟（每种机型一个世界、各 1 架次；需先确保共享锁可用）：

```bash
PYTHONPATH=src:. PYTHONUTF8=1 bash _scratch/sitl_lock.sh -- bash -lc 'python scripts/collect_sim_teacher_episodes.py --scenario configs/scenarios/generated-r20/gen_r20_w0000.scenario.json --airframe x500_mono_cam --episodes 1 --steps-per-episode 120 --out artifacts/smoke/r20-multirotor'
PYTHONPATH=src:. PYTHONUTF8=1 bash _scratch/sitl_lock.sh -- bash -lc 'python scripts/collect_sim_teacher_episodes.py --scenario configs/scenarios/generated-r20/gen_r20_w0005.scenario.json --airframe rc_cessna_runway --takeoff-method runway --episodes 1 --steps-per-episode 120 --out artifacts/smoke/r20-fixed-wing'
```

提交 SITL 队列前可在离线环境执行同一 collector/stack-plan 构建覆盖（不会运行 Docker）：

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python -m pytest -q tests/test_sim_teacher_stack_preflight.py
```

整轮 dry-run 已实际运行（无采集/训练），退出码 0，命令阶段与低磁盘守卫均被打印。日志：`D:/Projects/mdwt/_scratch/cx-r20-worlds/dry-run-r20.log`；逐世界预检日志：`D:/Projects/mdwt/_scratch/cx-r20-worlds/preflight-r20.log`。可复用命令：

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
& 'D:/Projects/Multi-Detect/.venv-vlm/Scripts/python.exe' scripts/run_r15_round.py `
  --dry-run --stage all --low-disk `
  --out D:/Projects/mdwt/_scratch/cx-r20-worlds/pipeline-r20 `
  --episodes D:/Projects/Multi-Detect/artifacts/r20-round/episodes `
  --scenarios configs/scenarios/generated-r20 `
  --r15-index configs/scenarios/generated-r20/index.json `
  --source-root D:/Projects/Multi-Detect
```

本次 dry-run 退出码为 0，所有阶段命令与低磁盘守卫均打印。资源守卫报告可用磁盘约 48.0 GB；low-disk collect 估算峰值 8.77 GiB，训练阶段注明至少 30 GiB，export 阶段至少 15 GiB。onboard gate 完整结果记录于 `D:/Projects/mdwt/_scratch/cx-r20-worlds/onboard-gate-r20.log`：1516 passed、2 skipped、21 deselected，PASS（202 秒）。

## SITL 架次几何与离线验收

固定翼 `gen_r20_w0006..w0010` 使用 30 m 观察高度和 80 m PX4 loiter/orbit
半径；固定翼专家直接发送以该观察航点为中心的 orbit 命令，不经过多旋翼式逐点
translate。PX4 建立盘旋后在目标进入前视 FOV 时采帧，持续保持 30 m 意图高度。每个位姿至少 8 个 `target_in_fov` 帧，目标地面距离按 80 m 盘旋设计档位
验收（±25%），高度按意图值 ±3 m 验收。多旋翼仍按各 sidecar waypoint 的 8 帧、
高度和地面距离档位验收。

一个 `expert_search` 架次按 sidecar 的 `observation.camera_altitude_m` 和 `observation_waypoint` 飞到观察点；目标 ground distance 是 waypoint 到 truth 真目标的水平距离。观察俯仰由 intent 指定；无云台时采集器用等效机身/高度与距离几何实现。每个观察位至少留下 8 帧有效目标观察，不能因起飞点首次检测到目标而结束。多旋翼到位观察时高度须在意图值 ±3 m，水平目标距离须在设计档位 ±25%。固定翼验收按其 teacher 意图检查同样的有效观察帧、几何和完成状态。

固定翼单架次冒烟命令（串行、需外部 SITL 锁）：

```powershell
$env:PYTHONPATH='src;.'; $env:PYTHONUTF8='1'
python scripts/collect_sim_teacher_episodes.py --scenario configs/scenarios/generated-r20/gen_r20_w0006.scenario.json --teacher expert_search --airframe rc_cessna_runway --takeoff-method runway --episodes 1 --steps-per-episode 120 --out D:/Projects/Multi-Detect/artifacts/smoke/r20-fixed-wing
python scripts/check_sim_teacher_smoke.py D:/Projects/Multi-Detect/artifacts/smoke/r20-fixed-wing/episodes/<episode> --intent configs/scenarios/generated-r20/gen_r20_w0006.teacher_intent.json --truth configs/scenarios/generated-r20/gen_r20_w0006.truth.json --minimum-frames 8
```

离线验收（对已存在的 episode 目录运行，不启动仿真）：

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
python scripts/check_sim_teacher_smoke.py D:/Projects/Multi-Detect/artifacts/smoke/worldgen-20261025b/r20-multirotor/episodes/episode-0000-dc239359 --intent configs/scenarios/generated-r20/gen_r20_w0000.teacher_intent.json --truth configs/scenarios/generated-r20/gen_r20_w0000.truth.json --minimum-frames 8
```

验收脚本检查 episode complete、有效观察帧数、帧位姿高度均值和水平目标距离均值。`target_in_fov` 为真且 RGB 帧文件及姿态存在的观测才计入有效帧。通过标准是 ≥8 帧、高度误差 ≤3 m、距离相对误差 ≤25%；脚本输出 JSON 明细并以非零码报告失败。另须通过生成器离线出生点净空预检：出生点周围 15 m 安全半径无碰撞实体，垂直爬升廊道至观察高度无遮挡。
