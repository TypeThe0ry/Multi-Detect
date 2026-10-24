# 固定翼区域搜索超时分析（2026-10-25）

## 证据与时间线

本次离线分析使用 `artifacts/benchmarks/airframe-suite-20261024-fix/`（采集代码
`58a78317`）中的 `steps.jsonl` 和 `mavlink.jsonl`，没有修改 artifacts。

- `rc-cessna-w0016-ambulance-known-limit` 共 92 步。原语 `elapsed_s` 合计
  648.782 s，但 `timing.command_and_primitive_wait_ms` 合计约 677.87 s；步骤 0
  的 `motion_wait` 已超时 18.922 s，外层 outcome 却是 `success`、`elapsed_s=0.016`
  s。指令增量约 `(-24.1,-15.3,0)`，实际增量 `(-59.9,58.1,0.5)`，误差约
  81.6 m。步骤 1--13 是 1 s `hold` 对应的盘旋；步骤 13 后目标已在约 124 m
  外，步骤 14--91 反复超时（约 8 s/步），最后在 900 s 墙钟预算结束。
- `rc-cessna-w0031-ambulance-negative` 共 141 步，69 次 `translate`、72 次
  `hold`。原语耗时合计只有 5.084 s，实际 `command_and_primitive_wait_ms`
  约 700.85 s。步骤 3 的 wait 显示 `arrived`、距离 19.45 m，而 outcome
  仍为 `success`、`elapsed_s=0.047`；步骤 4 的 wait 已是 17.968 s timeout，
  outcome 仍为 `success`、`elapsed_s=0.032`，指令 `(-20.8,-19.5)` 与实际
  `(-23.2,62.0)` 相差约 81.6 m。整段 MAVLink 轨迹的中位水平速度约 15.2 m/s，
  不是停在原地。
- `AirframeMotionAdapter` 将固定翼 `hold` 映射为 `loiter/orbit`。因此 w0031 的
  72 次 hold 是绕圈，不是悬停；它们在 1 s 内仍有约 10--20 m 位移和连续航向变化。

## 根因分类

### 代码缺陷

1. `_wait_motion_command` 旧逻辑只判断沿目标航迹的投影 `remaining <= 5 m`。
   飞机从目标侧面穿过该投影平面时，即使横向误差 32 m 以上，也会返回
   `waited=arrived`。现在要求直接距离不超过 8 m，或飞过航点时同时满足沿程
   误差不超过 5 m、横向误差不超过 8 m。
2. `motion_wait=timeout` 原来只写入 ACK，步骤 outcome 仍沿用执行器的
   `success` 和约 0.05 s 的 ACK 耗时。现在超时会传播为 `status=timeout`，并把
   telemetry wait 时间加到 `elapsed_s`，离线记录与实际墙钟一致。
3. 当前 `FixedWingSearch` 已有固定翼盘旋捕获逻辑：在合理捕获半径内连续观察到
   abeam 且不再接近时推进航点；固定翼搜索高度修正也已叠加到飞行航段。相关行为
   用日志位姿重放测试覆盖。本次没有再改 `onboard_loop.py`。

### 配置与预算

搜索区域按 mission-suite 的半径 55 m 计算，面积约 `110 x 110 = 12,100 m²`。
固定翼相机 footprint 被规划器下限夹到 30 m，20% overlap 后航带间距约 22 m，约
5 条航带；`fw_geometry.lawnmower_search` 路径约 1,121 m。按 15 m/s 巡航，纯飞行
约 75 s；加转弯、每步感知和过顶观察，保守估计约 3--5 分钟，返航仍有余量。
因此当前 `max_episode_s=900`、`steps=160`（约 29--40 个 30--40 m 航段）并非
造成这次超时的预算根因，不上调预算。w0016 任务名为 `known-limit`，其文档已
记录固定翼斜视慢模型目前不能稳定确认救护车；suite 期望已明确为
`confirmed=false`、`min_safety_pass=true`。w0031 的 negative 任务保持同一期望。

## 修复验证

离线定点测试覆盖了日志中的横向穿越残差、合法 fly-through、超时状态传播，以及
固定翼搜索的盘旋捕获和 VTOL 兼容路径：

```powershell
$env:PYTHONPATH='src;.'; $env:PYTHONUTF8='1'
python -m pytest -q tests/test_fw_executor.py tests/test_fw_search.py tests/test_vtol_hover_ops.py tests/test_suite_airframes_resolve.py tests/test_suite_airframe_camera_contract.py
```

结果：`61 passed`。`git diff --check` 通过。未启动 SITL、Docker 或 llama-server。

## 排队的 SITL 复验

本机锁释放后只复验两个 Cessna 任务，各 1 架次；命令沿用套件调度器，锁必须包住
整个运行：

```bash
source /d/Projects/mdwt/_scratch/sitl_lock.sh
sitl_acquire airframe-suite-20261025-fw-search
trap sitl_release EXIT
export PYTHONPATH='src;.'
export PYTHONUTF8=1
python scripts/run_mission_suite.py \
  --suite configs/onboard/suite-airframes.json \
  --out /d/Projects/Multi-Detect/artifacts/benchmarks/airframe-suite-20261025-fw-search \
  --only rc-cessna-w0016-ambulance-known-limit rc-cessna-w0031-ambulance-negative \
  --episodes 1 \
  --min-free-disk-gb 8 \
  --strict
```

复验重点是：每个 `reposition` 的 `motion_wait` 不再以大横向误差返回
`arrived`；timeout 要出现在步骤 outcome；w0016 的期望是安全通过但不要求确认，
w0031 仍不得确认目标。

## SITL 复验（2026-10-25 01:34，代码 49bb467e）

| 任务 | 结果 | 说明 |
|---|---|---|
| rc-cessna-w0016-ambulance-known-limit | 10 步完成，确认救护车，位置误差 0.43 m，正常返航 | 完成判定修复后不再空转；原先“已知限制”期望（confirmed: false）已不成立，改回 `confirmed: true` |
| rc-cessna-w0031-ambulance-negative | 107 步后 900 s 超时；37 步 `motion_wait` 超时，始终停在 area_search | 完成判定已如实报告超时，但飞机到不了搜索航点，属于固定翼航点引导问题，需继续排查 |

结果目录：`artifacts/benchmarks/airframe-suite-20261025-fw-search/`。

## w0031 逐步还原与离线修复（2026-10-25）

本节使用代码版本 `49bb467e` 的原始 `steps.jsonl`、`mavlink.jsonl` 和 episode
记录；没有修改 `artifacts/`。w0031 有 107 条记录，其中 41 条 `translate`、66 条
`hold`，37 条 `motion_wait=timeout`。`translate` 的 `forward_m/right_m` 是机体系
增量；`labels.motion.target_ned` 是送给 PX4 的 NED 目标，`outcome.achieved_delta_ned`
是实际结果。

- step 0 从 `(65.34,46.08)` 发往约 `(40.50,32.11)`，命令增量
  `(-24.84,-13.97)`，实际 `(-53.66,+62.34)`，结束于 `(11.68,108.41)`，等待
  超时且距目标 `81.59 m`。
- step 2 是少数成功等待的 translate，距目标 `7.9 m`；step 3 从
  `(-24.77,68.01)` 指向约 `(-43.54,46.57)`，实际却移动 `(+27.17,+48.08)`，
  超时距目标 `83.38 m`。随后 step 4、5、6 的实际增量约为
  `(-99,-31)`、`(+28,+48)`、`(-83,-63)`，说明越过航点后在相邻航带之间来回折返，
  不是停在原地重试同一位置。
- heartbeat `custom_mode=0x03040000`（466/475 条）解码为 PX4 `AUTO_LOITER`，
  另有 9 条 `AUTO_TAKEOFF`，没有 OFFBOARD。每次 `SET_POSITION_TARGET_LOCAL_NED`
  的短目标都可能被固定翼按 loiter 处理；`hold` 期间速度约 15--20 m/s、航向连续
  变化，是盘旋而不是悬停。`WIND_COV` 风速分量约 `0.03--0.06 m/s`，高度约
  `29--31 m`，不足以解释轨迹；没有地形或坐标系异常证据。
- profile 的最小转弯半径为 `21.18 m`，转弯直径约 `42.35 m`。原搜索器按
  `0.9*max_leg` 抽样，`max_leg=30 m` 时航点间距约 `27 m`，小于转弯直径；而
  适配器每次 reposition 最多只给约 `28.5 m` 的短目标，和 PX4 的 loiter 捕获半径
  不匹配。这是航点生成/到达语义的固定翼几何缺陷；不是 OFFBOARD、风或地形问题。

w0016 也有前两次 translate 超时（距目标约 81/80 m），但随后连续 7 次 hold 盘旋，
在 step 8 获得目标确认（位置误差 `0.43 m`、IoU `0.985`），step 9 返航完成。w0016
搜索半径约 `150 m`（`300 x 300 m`、约 `90,000 m²`），首条观测位置约
`(56.4,56.3)`，位于区域内部但偏离中心；w0031 半径约 `55 m`（`110 x 110 m`、
约 `12,100 m²`），首条位置约 `(65.3,46.1)`，已在边界附近/区域外侧。w0016 比
w0031 面积大约 7.4 倍，能提供更长航段和更宽松的搜索空间。成功来自盘旋观察时获得
目标证据，不能证明短 reposition 本身可靠。w0031 的 `mission_summary.json` 将计划中的
`area_search` 和 `return_home` 都记为 `unknown/no_recorded_completion`，所以
`skills=False` 是两个计划技能没有完成记录；失败 campaign 的 report 没有 episode
级 safety 子项，实测最低相对高度约 `28.6 m`、返航距离未超过 `300 m`，因此
`min_safety_pass=False` 应归因于返航未完成/未记录，而不是低高度或碰地。

离线修复在 `FixedWingSearch` 中完成两点：航点抽样间距至少为
`2*min_turn_radius`；同一航点在捕获范围内连续多次没有沿程进展时推进到下一个航点，
避免 AUTO_LOITER 永久追逐一个不可精确到达的点。后者保留了“远处或仍在接近时不跳点”
的约束，并增加了定点测试。它不能替代 PX4 SITL 对 fly-over/capture 参数的最终确认，
但能离线证明搜索器不会因单点盘旋无限卡死。

## 待排队的 SITL 复验

锁释放后只跑 w0016 和 w0031 各 1 架次：

```bash
source /d/Projects/mdwt/_scratch/sitl_lock.sh
sitl_acquire airframe-suite-20261025-fw-search
trap sitl_release EXIT
export PYTHONPATH='src;.'
export PYTHONUTF8=1
python scripts/run_mission_suite.py \
  --suite configs/onboard/suite-airframes.json \
  --out /d/Projects/Multi-Detect/artifacts/benchmarks/airframe-suite-20261025-fw-search \
  --only rc-cessna-w0016-ambulance-known-limit rc-cessna-w0031-ambulance-negative \
  --episodes 1 --min-free-disk-gb 8 --strict
```

复验检查：w0031 的同一搜索航点不能连续超时后永久保持同一 `_index`，轨迹应继续进入
后续航带；w0016 仍应安全返航，w0031 不得将未确认目标报告为 confirmed。
