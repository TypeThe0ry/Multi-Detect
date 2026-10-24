# 任务执行器不变量随机测试发现清单

## 方法

`tests/test_mission_executor_fuzz.py` 使用 `random.Random(seed)` 生成可复现场景，复用现有 `OnboardLoop` 假感知和 NED 运动学积分器，不启动 SITL。场景覆盖矩形 operating area（作为包络多边形）、0--3 个禁飞圆、随机高度上下限、包 home 与首个空中位姿不同、`area_search`/`count`/`patrol`/`monitor`/`follow`，以及目标出现、消失、移动、身份判断翻转和感知丢帧。

每一步检查安全出口后的实际位姿和整段运动：不得越过 operating area、膨胀禁飞圆或高度上下限；任务必须在配置预算加返航尾段内结束；失败/预算耗尽必须经过 `return_home`；报告位置必须与 findings 一致；并检查包 home 已绑定到 `SafetyEnvelope`。

默认 gate 跑 40 个 seed。完整 300 个 seed：

```powershell
$env:PYTHONPATH = "src;."
$env:MISSION_FUZZ_SEEDS = "300"
python -m pytest -q tests/test_mission_executor_fuzz.py
```

失败 seed 的最后一份场景和轨迹写入 `D:/Projects/mdwt/_scratch/cx-executor-fuzz/last_fuzz_trace.json`。该文件是临时输出，不纳入提交。

## 结果

| seed 范围 | 结果 | 真实缺陷 | 测试模型问题 |
| --- | --- | --- | --- |
| 0--39 | 通过 | 无 | 无 |
| 0--299 | 通过 | 无 | 无 |

本轮随机场景没有产生可稳定复现的执行器缺陷，因此没有伪造回归修复或修改生产逻辑。已有的首个位姿高度基准、包 home 绑定、计数预算返航和 follow 越界修复均在随机场景中纳入断言。

早期模型把所有场景（包括禁飞区阻断的返航和 follow 丢失目标）都计入“正常巡航拒绝率”，得到 seed 4 的 77% 拒绝率。该判定把预期的 fail-closed 返航拒绝误报为系统性误拒；模型已修正为仅在无禁飞区的 patrol/monitor 正常巡航场景统计，属于测试模型问题，不是执行器缺陷。

## 变异测试证据

`tests/test_mission_executor_fuzz.py -k mutation` 使用 `monkeypatch` 注入变异。每个变异都必须先触发原本应失败的不变量断言，测试再用 `pytest.raises(AssertionError)` 确认测试确实抓到了它；修复后的源代码和原始 300 seed campaign 均通过。

默认 onboard gate 只收录主随机测试以及 a、d 两个轻量变异；b、c、e、f 标记为 `mutation_full`，完整变异复核命令为 `python -m pytest -q tests/test_mission_executor_fuzz.py -m mutation_full`。

| 变异 | monkeypatch 改动 | 失败场景/断言 |
| --- | --- | --- |
| a. 首个位姿高度当 home | 令 `_init_home` 在正常初始化后调用 `safety.set_home((pose.n, pose.e, pose.down))`；使用 seed 1 的无 package、adapter 已配置包络、首个位姿空中巡航场景 | seed 1；`rejected / actions <= 0.2` 失败，24/25 个动作被 `min_altitude` 拒绝并原地 hold |
| b. 包 home 不绑定包络 | 构造器完成后把 `safety.home_ned` 改回错误的 `(0, 0, -30)` | seed 2；精确 home 绑定断言失败：`tuple(home_ned) == (*package_home, 0.0)` |
| c. count runtime budget 不受限制 | monkeypatch `_count_budget` 返回 `count_budget_max`，配置 `runtime_step_limit=12`、返航 reserve=3 | seed 3；总 primitive 数断言失败：变异消耗超过 runtime limit，不能保证返航尾段 |
| d. follow 越过 operating area | monkeypatch `_follow` 返回 100 m 越界 translate，并让 follow 安全出口直接放行，模拟 boundary observe/guard 被回退 | seed 4；实际积分后的位姿 `_inside(operating_area)` 断言失败 |
| e. 去掉禁飞区 segment 检查 | monkeypatch executor 的 `_translate_enters_no_fly` 和 adapter guard 放行，seed 5 从 `(-8, 0)` 直穿中心禁飞圆 | seed 5；动作终点到禁飞圆中心距离小于膨胀半径 6 m，禁飞不变量失败 |
| f. 去掉失败后的 return_home 链接 | monkeypatch `_finish` 在 failed 时直接把任务置 terminal，不执行 `on_fail` | seed 6；失败路径最后必须出现 `return_home` 的断言失败 |

这里没有使用源码文件临时改写；变异全部在 pytest 内 monkeypatch。当前源码没有名为 `boundary_observe` 的函数，follow 的实际边界保护由 `AirframeMotionAdapter.guard_command` 进入 `SafetyEnvelope` 完成，因此 d 变异针对这个等价安全出口被移除的行为。

## 处置与复核

随机测试本身加入 onboard gate 的快速子集，完整 campaign 通过环境变量开启。由于没有真实失败 seed，没有可供 `git stash` 前后对照的回归测试；定点回归仍由现有 mission executor/no-fly 测试覆盖。

Gate 首次在 Windows 默认 GBK 编码下运行时，3 个现有 `test_onboard_real_mode_harness.py` 用例读取 UTF-8 示例包触发 `UnicodeDecodeError`，与本次改动无关。复跑时设置 `PYTHONUTF8=1`，让解释器按 UTF-8 读取仓库文本；最终 gate 结果以 scratch 日志记录为准。
