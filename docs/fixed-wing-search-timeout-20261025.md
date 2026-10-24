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
