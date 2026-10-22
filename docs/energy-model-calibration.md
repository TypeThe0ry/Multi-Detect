# SITL x500 电池遥测与机载能量模型

结论：PX4 SITL x500 的电池遥测里**没有任何可用于标定功率或容量的物理量**，因此不新增
SIM 专用能量 profile，`EnergyConfig` 缺省值（360/400/500 W 功率下限、14.8/13.2 V
额定/截止电压）保持不变。本文件记录依据、SIM 下模型的实际行为和由此推出的返航时间，
供编写 SIM 任务包时核算 `energy_wh`。它不是实机标定；实机流程见
[onboard-energy.md](onboard-energy.md) 的“实机校准流程”。

## 数据与核对方法

2026-10-23 只读扫描了 `D:/Projects/Multi-Detect/artifacts/benchmarks/**/mavlink.jsonl`
与 `D:/Projects/mdwt/_scratch/e2e/run*/**/mavlink.jsonl`，共 189 个日志，其中 185 个含
电池消息（`BATTERY_STATUS` 23,486 条，`SYS_STATUS` 47,073 条）。未启动 SITL，未改动日志。

| 字段 | 全部样本的取值 |
| --- | --- |
| `BATTERY_STATUS.current_battery` / `SYS_STATUS.current_battery` | 恒为 `-1`（未知） |
| `current_consumed` | 恒为 `0` mAh |
| `energy_consumed` | 恒为 `-1`（未知） |
| 总电压（`voltages[0]`） | 15.300–16.200 V |
| `battery_remaining` | 50–100 % |
| `BAT1_CAPACITY` | `-1`（`docs/evidence/onboard-teacher-px4-diagnostics-20261008-r1.json`） |

逐条核对电压与电量的关系：所有样本满足 `V ≈ 14.4 + 1.8 × pct/100`，最大偏差 0.054 V
（约 3 个百分点，来自电量取整和电压滤波）。

PX4 源码 `src/modules/simulation/battery_simulator/BatterySimulator.cpp`（上游 main）给出了
原因：电量在解锁期间按 `SIM_BAT_DRAIN` 秒线性下降、解锁前重置为 100 %、下限钳在
`SIM_BAT_MIN_PCT`；电压由该电量在空/满单体电压之间线性插值再乘以单体数；电流固定写
`-1.0f`（源码注释为“no current sensor in simulation”）。本项目参数为 `SIM_BAT_DRAIN=60`、
`SIM_BAT_MIN_PCT=50`（批量运行），operator 会话由 `scripts/run_sim_session.py` 改为 95。
由 16.2 V/100 % 与 15.3 V/50 % 反推单体电压为 3.6–4.05 V、4 节，空包对应 14.4 V。

因此：

- 悬停、平移、爬升功率与可放出 Wh **无法从 SITL 识别**。360/400/500 W 仍是未标定的
  保守下限，不是 SITL 测量值，也没有证据表明它偏高或偏低。
- SITL 电压不是独立的负载测量，只是合成电量的线性函数；它不能再作为“压降”证据。
- 估计器对这些哨兵的处理是正确的：`-1` 电流不会成为功率样本，`0 mAh` 计数器不增加
  任何约束，`-1` 能量字段被忽略（`tests/test_energy_estimator.py` 的
  `test_px4_sitl_battery_adds_no_power_counter_or_voltage_sag_evidence`）。

## 已放弃的 `sitl_x500` profile

上一版草稿曾在 `runtime_mode="sim"` 时自动改用 16.2/14.8 V 额定/截止电压。复核后整体丢弃：

1. 14.8 V 截止电压没有观测依据（日志最低 15.3 V，源码推得空包 14.4 V）。
2. 压降系数 `(V - cutoff)/(nominal - cutoff)` 会把已经计入 SOC 的合成电量再乘一次：
   15.3 V 时系数为 0.357，且任务内只降不升。用同一组 SITL 遥测回放（200/40 Wh 任务包、
   距 home 30 m、高 20 m），`SIM_BAT_MIN_PCT=50` 时第 46 s 就触发 `return_now`，
   现行缺省配置为第 440 s；`SIM_BAT_MIN_PCT=95` 时为 1022 s 对 1044 s。
3. 自动选择会静默覆盖 onboard policy `energy_model` 中显式配置的电压。

功率、容量、风裕量、安全系数等参数也不做任何 SIM 放宽：放宽只会让 SIM 掩盖本应暴露的
预算不足，同时与实机路径产生分叉。

## SIM 下模型的实际行为

每条带有效电量的电池消息都是一个能量锚点 `capacity_wh × pct/100`；估计器取各锚点
减去此后按最大阶段功率（缺省 500 W）积分的消耗中的最小值。SITL 合成电量在解锁后按
`SIM_BAT_DRAIN=60` 约 1.7 %/s 下降（200 Wh 任务包即约 3.3 Wh/s），远快于 500 W 下限（0.14 Wh/s），所以锚点由合成电量决定，钳住后再按 500 W 扣减。返航在
`剩余 ≤ 预留 + 返航能量` 时触发，钳住之后的可飞时间约为：

`T ≈ (energy_wh × SIM_BAT_MIN_PCT/100 − 预留 − 返航能量) × 3600 / 500 秒`

其中 `预留 = max(reserve_energy_wh, 500 × reserve_time_s / 3600)`，返航能量按
onboard-energy.md 的公式（含 1.3 安全系数）。下表按返航能量约 6 Wh（30 m 外、20 m 高）
估算，未计钳住前的数十秒：

| 任务包 | 预留 | `MIN_PCT=50` | `MIN_PCT=95` |
| --- | --- | --- | --- |
| suite / 示例 200 Wh、40 Wh | 40 Wh | 约 6.5 min | 约 17.3 min |
| count-cones 示例 120 Wh、30 Wh、`reserve_time_s=180` | 30 Wh | 约 2.9 min | 约 9.4 min |
| e2e operator 600 Wh、60 Wh、`reserve_time_s=180` | 60 Wh | 约 28 min | 约 60 min |

`test_px4_sitl_endurance_is_the_package_budget_at_the_power_floor` 用 200/40 Wh 固定了
第一行在两种钳位下的行为（440 s 与 1044 s，含钳住前时间）。这些时长只由合成排放参数和任务包
`energy_wh` 决定，与真实耗电无关：

- 要让 SIM 任务飞得更久，应在任务包里按上式给出足够的 `energy_wh`，或像 operator 会话
  那样调 `SIM_BAT_MIN_PCT`；不要改能量模型参数。suite 的“电量预留返航”任务依赖缺省排放。
- SIM 里因能量返航只说明任务包预算按保守下限不够，不说明 x500 实机续航不足或充足。
- SIM 的电池时效仍按 PX4 启动时钟判定，整条链路断档（`note_delivery_gap`）不计入电池
  年龄，恢复后 5 s 内无电池消息照常返航；本文不改变这些规则。

## 局限

- 没有 x500 实机的电流、回充能量或 `current_consumed` 记录，仓库内也没有可核验的实机
  功率/续航数据；不推断实机瓦特数或容量。实机必须按 onboard-energy.md 的流程，用校准过的
  电流计数据配置 `energy_model`。
- 扫描结果对应 2026-10-23 时磁盘上的日志集合；日志后续增删会改变计数，但不会改变
  `current_battery=-1`、`current_consumed=0` 这类由 PX4 源码决定的结论。
- 若将来换用带电流模型的仿真电池（例如自定义 Gazebo 电池插件），需重新核对本表并评估是否
  能拟合功率；在那之前任何 SIM 功率数值都不应写入配置。
