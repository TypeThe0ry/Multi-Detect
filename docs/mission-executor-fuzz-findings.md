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

## 处置与复核

随机测试本身加入 onboard gate 的快速子集，完整 campaign 通过环境变量开启。由于没有真实失败 seed，没有可供 `git stash` 前后对照的回归测试；定点回归仍由现有 mission executor/no-fly 测试覆盖。

Gate 首次在 Windows 默认 GBK 编码下运行时，3 个现有 `test_onboard_real_mode_harness.py` 用例读取 UTF-8 示例包触发 `UnicodeDecodeError`，与本次改动无关。复跑时设置 `PYTHONUTF8=1`，让解释器按 UTF-8 读取仓库文本；最终 gate 结果以 scratch 日志记录为准。
