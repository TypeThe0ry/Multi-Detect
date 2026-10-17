# 机载能源与返航余量

能源估计器在机载侧使用 MAVLink 电池、位置、速度和风信息，为 MissionSupervisor
提供可用能量、返航所需能量、触及预留量前的时间和 action margin。决策不依赖地面站。
触及返航余量时应进入 `return_home`；连不含预留量的返航消耗也无法满足时应进入
`land_now`。任务包的时间预算始终是额外约束；没有有效电池数据时，仅使用该时间预算，
日志必须明确标注回退原因，不能声称已测得电池续航。

返航估计分解为水平航程、爬升、下降、落地和预留。水平时间使用巡航速度，并对逆风做
保守修正；缺少风估计时使用配置的未知风裕量。各阶段功率使用最近有效电压×电流样本，
并受配置的功率下限约束。必须把功率下限、能量容量和电压阈值视为机型参数，而不是通用
航空常数。突发电压下降应降低可用能量，电压短时回弹不应恢复已经扣除的安全裕量。

## 接线、单位与配置

`sim_teacher.streams.MavlinkReader` 保存 `BATTERY_STATUS`、`SYS_STATUS`、`WIND_COV`、
`HEARTBEAT` 和 `LOCAL_POSITION_NED`，`build_observation()` 将原始接收单调时间连同
`energy_telemetry` 传给 onboard policy，再调用 `MissionSupervisor.ingest_energy_telemetry()`。
直接接入飞控的机载进程也可以调用该方法；缓存消息必须保留原始时间，不能用每帧时间刷新电池。
只接受配置的系统、组件及电池 ID（默认 1/1/0）；同一任务内不因解除武装、计数器归零或 SOC
回升而补能。多电池系统须先提供单一、经过验证的总能量来源，不能把不同 pack 混算。

策略配置文件可在 `mission_package` 旁增加 `energy_model`（任务包自身的 budget 结构不变）：

```json
{
  "energy_model": {
    "capacity_wh": 100,
    "nominal_voltage_v": 14.8,
    "cutoff_voltage_v": 13.2,
    "hover_power_w": 360,
    "translate_power_w": 400,
    "climb_power_w": 500,
    "cruise_speed_mps": 5,
    "climb_speed_mps": 2,
    "descent_speed_mps": 1,
    "landing_time_s": 15,
    "unknown_wind_mps": 2,
    "safety_factor": 1.3,
    "telemetry_timeout_s": 5,
    "power_window_s": 60
  }
}
```

这些数值是配置示例，需实机标定。未单独配置时 capacity 使用任务包 `energy_wh`；
配置的预留 Wh/秒只能加严任务包的 reserve。SOC 将容量映射为 Wh，已消耗 mAh 按配置额定电压
转换并约束余量；计数器自启动归零不证明起飞满电，因此需要有效 SOC 锚点才估计剩余能量。
两次输入之间按最大阶段功率与最近实测功率的较大值保守扣账，计数器/SOC 采用上界取小，避免
把积分和计数器直接相加而重复扣电。旧消息须从其原始接收时刻投影到当前时刻。

只在新鲜解锁和速度状态下学习最近 60 秒阶段功率：`vz < -0.5 m/s` 为 climb，水平速度
大于 `1 m/s` 为 translate，其余为 hover；下降按 hover 功耗估算。样本平均值不能低于配置
功率下限。默认总电压低于 14.8 V 时以至 13.2 V 的线性系数降低余量，该系数在任务内只会变严。
未知/负值/NaN 不成为零功耗；超时后已有能量估计继续扣账，并在仍能返航时立即请求返航。

`return_energy_wh = safety_factor × (P_translate × 水平秒数 + P_climb × 爬升秒数
+ P_hover × (下降秒数 + landing_time_s)) / 3600`。返航高度使用执行器巡航高度；
NED 的高度原点假设为 home 地面，水平路径假设可沿直线返航，绕障/地形绕行须提高安全系数或
由调用方提供覆盖实际航程的距离。WIND_COV 使用 NED 水平风，逆风和侧风降低地速，不兑现顺风收益；
未知风使用配置裕量。风超过可建模巡航能力时请求 `land_now`。

预留取配置 Wh 与最大阶段功率对应 reserve 秒数的较大值。
`action_margin_wh = 剩余能量 - 预留能量 - 返航能量`；非正且返航可达时请求 `return_now`
（执行器映射为 `return_home`），剩余能量小于返航能量时请求 `land_now`。
`time_to_reserve_s` 表示扣到预留前的保守时间，`action_margin_s` 表示 margin 对应的时间。
无 SOC 锚点时 Wh 字段为 null、来源为 `mission_time_budget_only`，秒数来自任务时间预算，
不能解释成电池实测续航。`decision.energy` 与 execution state 保存上述数值与降级原因。
返航/降落请求沿用现有 executor、安全层和 PX4 保护，不改变底层自动着陆能力或绕障能力。

## 现有 SIM 日志的校准结果

证据文件为 `docs/evidence/onboard-energy-sim-calibration.json`，每个输入日志记录完整路径、
SHA-256、字节数及电池样本数量。固定范围是现有
`artifacts/benchmarks/mission-suite-v1-r12` 中按路径排序的前 16 个 `mavlink.jsonl`。
其中 14 个日志有电池数据，另外 2 个日志没有电池消息。没有启动 simulator、Docker 或 GPU，
也没有修改原始日志。

| 项目 | 观测结果 |
| --- | --- |
| BATTERY_STATUS | 1,071 条 |
| SYS_STATUS | 2,153 条 |
| WIND_COV | 0 条 |
| BATTERY_STATUS 总电压 | 15.300–16.200 V |
| BATTERY_STATUS 剩余电量 | 50–100%，其中 824/1,071 条为 50% |
| SYS_STATUS 剩余电量 | 有效值集中在 51% 等，含 -1 未知哨兵 |
| current_battery | 两种电池消息所有样本均为 -1，表示未知 |
| current_consumed | 所有 BATTERY_STATUS 样本均为 0 mAh |
| energy_consumed | 所有 BATTERY_STATUS 样本均为 -1，表示未知 |
| time_remaining | 所有 BATTERY_STATUS 样本均为 0，表示不可用 |

`LOCAL_POSITION_NED` 可划分运动阶段：hover 44,322 条、translate 33,469 条、climb
11,594 条、descent 18,182 条。这些是速度消息样本数，包含未解锁/地面状态，不能解释成
各阶段有效功率样本数。所有阶段的有效瓦特样本数均为 **0**，平均功率为 **null**。

因此这批日志只能校准输入兼容性与有效范围，**无法拟合真实 hover/translate/climb 功率**。
不能把 `current_battery=-1` 当作零电流，也不能把一直为 0 的 `current_consumed` 当作
零功耗。日志中电量下降后长时间停在 50%，电压停在 15.300 V，显示 SITL 电池模型非常
简化；SYS_STATUS 与 BATTERY_STATUS 的 50/51% 差异也不能用于推断真实容量。

目前 hover/translate/climb 功率下限为 360/400/500 W；它们是**未实测的配置缺省值**，
不是上述 SIM 数据拟合结果。默认 nominal/cutoff 电压 14.8/13.2 V 与本批 4S 电压量级一致，
但单凭 16.2 V 无法证明实际电芯数、化学体系或截止电压。PX4 可将整包电压放在
`voltages[0]`，其余槽位为 65535；不能按有效数组项数量推导电芯数。实机必须通过
onboard policy 的 `energy_model` 明确配置容量、额定/截止电压和阶段功率。

复现命令（从当前 worktree 根目录执行）：

```powershell
python scripts/calibrate_onboard_energy.py --input-root D:\Projects\Multi-Detect\artifacts\benchmarks\mission-suite-v1-r12 --max-logs 16 --output docs/evidence/onboard-energy-sim-calibration.json
```

脚本只读输入日志，输出路径强制限制在当前 worktree。扫描范围、消息总数、哨兵值、
阶段分类阈值及每个日志的 SHA-256 均保留在 JSON 中。不同时间若原始日志发生变化，
应以 SHA-256 区分证据版本，而不是直接比较汇总数量。

## 实机校准流程（必须执行）

### 三种机型

机型参数通过 `EnergyParams` 传入，主线程可以把 `AirframeProfile` 的值转换成这个轻量对象。默认 `category="multirotor"`，旧的 360/400/500 W、5 m/s 和返航公式完全不变。

- 多旋翼继续按悬停、平移、爬升分段积分，返航包含爬升、下降和悬停降落。
- 固定翼按巡航、盘旋、爬升、下降和进近分别计费。返航距离额外加入进近航线和至少一个最小转弯半径的盘旋圈；风同时影响地速和横风可达性。`glide_distance_m` 用当前高度、巡航空速和滑翔下沉率估计无动力还能飞多远，不能当作有动力返航保证。
- VTOL 先估算固定翼航段，再加入转换功率和多旋翼降落功率。悬停段使用显著更高的机型悬停功率，能量不足时直接给出 `land_now`，不把不可完成的转换当作可达返航。

新增诊断字段 `return_segments`、`route_distance_m`、`minimum_loiter_distance_m`、`glide_distance_m` 只在固定翼和 VTOL 输出；这样默认多旋翼的结果结构和数值保持兼容。

1. 固定机型、螺旋桨、有效载荷、电池型号/电芯数、环境温度及软件版本；先在受控台架上
   用经过校准的电压表和电流计核对 PX4 的电压、电流方向、单位与 BATTERY_STATUS
   电池 ID。电流的 MAVLink 单位是 cA，电压是 mV，已消耗容量是 mAh。未知哨兵必须过滤。
2. 在已批准的受控飞行范围记录完整 MAVLink 和飞控日志，至少覆盖稳定悬停、多个速度的
   水平平移、爬升、下降和落地。每种阶段做多次重复，覆盖重载、较低电量、逆风和温度变化；
   记录地面/解锁状态，排除未起飞样本及传感器失效段。
3. 时间对齐电池电压/电流、LOCAL_POSITION_NED 或 GLOBAL_POSITION_INT、飞行阶段和
   WIND_COV。逐样本计算 `P=V*I`，积分 `Wh=sum(P*dt)/3600`，与充电器回充量及
   `current_consumed` 交叉核对；对采样间隙、复位及多电池 ID 分段处理。
4. 对各阶段按实际飞行速度、垂直速度和载荷统计平均功率、较高分位数和最差重复值；
   保守选取功率下限、速度和安全系数。容量使用实际可放出的能量扣除老化/低温降额，
   截止电压采用带载电压，不能拿空载恢复电压重新增加可用余量。
5. 通过离线回放验证距离越远越早返航；覆盖逆风、突然压降、缺失/过期电池消息、传感器
   噪声、返航不足和任务时间预算到期。检查机载 decision 日志中的 Wh、time-to-reserve、
   action margin 和 `return_home`/`land_now` 原因是否符合预期。
6. 先在近距离、充足余量下实飞验证预测返航/落地消耗与实际积分消耗，逐步扩展范围；
   保留预测误差与配置版本，按误差增加余量。在得到这些证据前，不能将 SIM 通过解释成
   实机续航安全已经完成校准。
