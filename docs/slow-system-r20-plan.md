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
| 干扰物成对 | 6 | ambulance→bus（6） |
| 固定翼高空斜视 | 5 | fire_truck→ambulance（5） |
| 背包远小斜视 | 5 | backpack→cardboard_box（5） |
| 消防栓建筑遮挡/下沿截断 | 4 | fire_hydrant→cardboard_box（4） |

所有真目标与 v2 held-out 组合无交集；SUV/pickup 不作为真目标。固定翼组使用 fixed-wing-v2 已有的 `px4_fixed_wing_gazebo_v1` 机体和相机/深度/空速传感器，目标按约 101 m 斜距与高空 spawn/下俯姿态布置。背包组采用约 19 m 相机高度、约 28 m 地面距离；消防栓组在相机与目标视线中点放置 12 m 建筑实体，并配置高位下俯相机。上述为配置几何验收，未进行渲染或 SITL 采集验证。

逐世界 `build_stack_plan(scenario, repository_root=Path.cwd())` 预检 20/20 通过，SDF 可解析且三路 channel hashes 与内容一致。新增回归测试逐真目标检查 held-out 泄漏并固化四类分布与几何断言。此前 6284094c 的旧三模板资产由本次新提交文件覆盖；历史提交未改写。

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

本次 dry-run 退出码为 0，所有阶段命令与低磁盘守卫均打印。资源守卫报告可用磁盘约 32.8 GB；low-disk collect 估算峰值 8.77 GiB、结束后余量 2.77 GiB，训练阶段注明至少 30 GiB，export 阶段至少 15 GiB。onboard gate 完整结果记录于 `D:/Projects/mdwt/_scratch/cx-r20-worlds/onboard-gate-r20.log`：1516 passed、2 skipped、21 deselected，PASS（115 秒）。
