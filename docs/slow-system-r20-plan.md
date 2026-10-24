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

## 世界与分布

`scripts/generate_r15_worlds.py --profile r20` 是隔离开关，默认行为不变。r20 profile 生成三类世界：pickup+SUV、cone+jersey barrier、backpack+cardboard box/消防栓。世界索引记录 `hard_case_profile`、`paired_confusers` 和 `view_geometry`；固定翼元数据覆盖坡度 20--40 度、斜距 80--200 m、框短边 20--40 px，背包覆盖 18--20 m 高、25--35 m 斜距、10--13 px。正式集合不改，样例写入 `_scratch/cx-r20-data/worlds`。

补采/后续扩展必须继承 `configs/scenarios/generated-v2/index.json` 的 `held_out_target_combos`，不得把 test 组合回灌 train。先按世界、目标类、候选类和尺寸分层，再合并；每个反事实正例都要有同帧拒绝。

## 门禁与验收

离线阶段检查：所有世界 truth 与 index 可解析；每个 paired world 至少一个正类和一个标注 confuser；r20 规划正确样本 `check_plan_intent` 违规数为 0，错误样本分别命中 `return_intent_land` 和 `ungrounded_place`。单元测试覆盖 profile 参数、配对字段、默认兼容性和 CLI 开关。

候选模型验收必须同时满足：v1 干扰物拒绝率 >= r18 0.974；pickup/SUV 与 cone/barrier 配对拒绝率 >= 0.97；16--20 px 拒绝率 >= r18 对应切片；固定翼车辆拒绝率不低于 v1 基线；规划返航和未知地点拒绝 100%。任一身份拒绝门禁下降即否决 r20，即使小目标召回提升也不替换 r18。

## r20 世界资产生成与本地验证（2026-10-24）

按固定 seed `20261024` 运行已合并的 `scripts/generate_r15_worlds.py --profile r20 --count 20 --world-prefix gen_r20_w`，资产落在 `configs/scenarios/generated-r20/`、`simulation/gazebo/worlds/`、`simulation/maps/`、`simulation/terrain/`。为兼容 `run_r15_round.py collect` 固定要求的 20 世界，索引划分为 16 train / 4 val / 0 test；`held_out_target_combos` 原样继承 `generated-v2/index.json`。

逐世界用 `build_stack_plan(scenario, repository_root=Path.cwd())` 预检 20 个 SIM stack，全部通过；这只构建配置计划，不启动 SITL、Docker 或仿真。r20 当前 profile 仍只有 3 个实际生成模板，循环后计数为：pickup/SUV 7 世界、construction_cone/jersey_barrier 7 世界、backpack/cardboard_box 并强制 fire_hydrant 6 世界。索引另附的固定翼斜视坡度/斜距/像素范围是模板元数据，不是经生成器实现的相机几何；当前模板也没有消防栓作为真目标的东北斜视下沿截断世界。故此资产集不宣称已覆盖方案的四种实际几何切片。

**正式训练前置阻断：** r20 profile 生成的 7 个 pickup 真目标组合与 v2 留出的 `pickup/null` 重合，属于 train/val 泄漏；另外 SUV 在配对中出现为干扰物，但不作为真目标的泄漏。现有 profile 也不支持将 pickup/SUV 配对仅作为负类同时保留该 holdout。该资产集仅用于离线 SIM stack 预检和轮次编排 dry-run，不能投入采集或训练。需先扩展 profile：实现 pickup/SUV 配对负样本表达且排除 held-out true target、加入真实固定翼斜视与消防栓截断模板，再重新生成并复验。

整轮 dry-run 已实际运行（无采集/训练），退出码 0，命令阶段与低磁盘守卫均被打印。日志：`D:/Projects/mdwt/_scratch/cx-r20-worlds/dry-run.log`；逐世界预检日志：`D:/Projects/mdwt/_scratch/cx-r20-worlds/preflight.log`。可复用命令：

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONUTF8='1'
& 'D:/Projects/Multi-Detect/.venv-vlm/Scripts/python.exe' scripts/run_r15_round.py `
  --dry-run --stage all --low-disk `
  --out D:/Projects/mdwt/_scratch/cx-r20-worlds/pipeline `
  --episodes D:/Projects/Multi-Detect/artifacts/r20-round/episodes `
  --scenarios configs/scenarios/generated-r20 `
  --r15-index configs/scenarios/generated-r20/index.json `
  --source-root D:/Projects/Multi-Detect
```

注意 dry-run 采用资源守卫的当前主机可用空间估算，本次约 13.2 GB，而 export/training 文本预算分别要求 15/30 GiB；脚本仍将阶段命令全部列出。真正运行前必须满足这些守卫要求。
