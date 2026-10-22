# 机载 AI 无人机操作手册

完整的 SIM prompt-to-flight 操作步骤见 [SIM 操作员端到端指南](e2e-operator-sim.md)。

这是一份给飞手和地面站操作员看的手册。它描述当前软件能做什么、怎样准备任务、失联时会怎样，以及怎样读结果。当前能力来自 Gazebo + PX4 仿真；尚未在 Jetson Orin Nano 或真实飞机上实测，不能把仿真结果当成飞行安全保证。

## 能做什么

机载系统支持 13 个技能：`goto` 前往地点、`area_search` 区域搜索、`count` 计数、`inspect` 多角度检查、`patrol` 巡逻、`survey` 网格测绘、`follow` 跟随、`monitor` 定点监视、`record` 记录证据、`deliver` 投递（默认关闭且尚未完成授权）、`hover` 悬停、`return_home` 返航、`land` 降落。

可以这样下达一句话任务：

| 中文 | English |
|---|---|
| 数一数场地里有几个交通锥，然后返航。 | Count the traffic cones in the field, then return home. |
| 找到邮筒，从四个方向拍照，然后返航。 | Find the mailbox, inspect it from four directions, then return home. |
| 监视场地五分钟，发现背包就报警。 | Monitor the field for five minutes and alert on backpacks. |
| 沿围栏巡逻一圈，注意有没有背包。 | Patrol one loop along the fence and watch for backpacks. |

当前诚实的仿真基准：单目标判断准确率 94.1%，干扰物拒绝率 89.9%；任务语义判定 8/12，任务规划 30 条较难新任务中中文 21/30、英文 25/30 完全正确。端到端仿真示例中，交通锥任务数到 2 个（0 次误报）并返航；这些数字不是实机保证。小目标、高处俯视、相似物体（例如电箱被当作背包）和中文任务仍会出错。

`deliver` 需要单独授权，当前不要用于真实投递。真实飞行、Orin Nano 性能、真实相机和真实降落区仍是待验证项目；相关功能目前只能视为 SIM-only。

## 机型与作业方式

机上 AI 的搜索、接近和确认会按机型选择动作方式：

| 机型 | 作业方式 | 起飞/返航要点 |
|---|---|---|
| 多旋翼 | 原地转向、前进、最近视角确认；确认后按任务计划返航 | 使用多旋翼搜索和接近流程 |
| 固定翼 | 按相机覆盖和转弯半径飞割草机搜索；发现候选后飞过目标边缘的顺时针过顶盘旋，每圈在目标前方取得一次视角 | 支持跑道、手抛、弹射起飞；确认后停止机上接近，由 PX4 RTL 返航并降落 |
| eVTOL 4+1、尾座式 | 默认 `vtol_hover_ops: true`，全程留在多旋翼（MC）模式，用多旋翼流程搜索、接近和确认 | 确认后由 PX4 RTL 返航并垂直降落 |
| 能悬停的 VTOL（可选混合） | `vtol_search_mode: "fw"` 先用固定翼割草机搜索，发现候选后回切 MC 接近和确认；候选被拒后在 MC 至少停留 3 步，条件满足时继续原固定翼航线。MissionExecutor 按当前步骤使用对应的区域搜索器；area_search/count/survey/patrol/monitor 可用 FW，inspect/land/return_home/deliver 始终用 MC | 每个任务最多回切固定翼 3 次；默认 `vtol_search_mode: "mc"` 不变 |

混合模式下，`vtol_fw_reentry_min_mc_steps` 调整被拒后的 MC 最少驻留步数，`vtol_fw_transition_budget` 调整允许回切 FW 的次数。`vtol_search_mode: "fw"` 只适用于能悬停的 VTOL；转换期间不推进机上 AI 循环。MissionExecutor 切换步骤时会清空混合状态，拒绝回切会退避且不消耗预算。

固定翼的单次确认不是“看到一次就算”：只有同一位置（跨航迹编号）的真实确认票多于拒绝票时，过顶的单次确认才算数；否则要等两次确认。尾座式在悬停时相机光轴比地平线抬高 20°，远处看得到、近处容易把目标放出画面，这是当前机型限制。

## 计数任务

计数搜索路线完成一整轮覆盖后结束，摘要原因显示为 `coverage_complete`，随后按计划返航。已完成一整轮覆盖时，数量不是“至少”值；若摘要原因是 `budget_exhausted`（步数预算用完）、`max_count` 或覆盖完成时仍有未回访的待确认目标，数量是下限（摘要里 `count_is_lower_bound` 为 true），因为还有区域或候选没有得到完整检查。

去重按位置记忆，而不是只看航迹编号：同一个地点换了新航迹编号不会再次接近；远处重见时也先按位置判断。两条航迹同框只有在之后都没有移开、且画面框没有嵌套时才作为两个目标的证据。摘要中的 `count_is_lower_bound`、`reason` 和证据帧要一起看。

## 定位精度

报告里的位置是目标在本地 N/E 平面上的估计位置，误差是该估计点到目标真值的水平距离，不是飞机位置，也不是目标边界框的像素坐标。单视 ground 定位通常打到大型目标的近边；`ground_extent_correction` 默认开启，但只有任务提供典型尺寸时才会沿底部射线把报告点推向预期中心。行人、标志、电线杆等“尺寸主要是高度”的目标，以及锥桶等小目标不做这项平移；多视角融合结果也不重复平移，关联仍使用原始地面锚点。

2026-10-20 的 SITL 结果可作为当前参考，不是实机保证：

| 场景 | 位置误差 |
|---|---:|
| 固定翼 w0016 救护车（两架） | 1.7 m、6.4 m |
| 固定翼 w0031 消防车（单视大目标修正后） | 3.2 m、2.7 m |
| standard_vtol（4+1） | 3.1 m |
| quadtailsitter（尾座式） | 4.8 m |
| standard_vtol 混合搜索（`vtol_search_mode: "fw"`），w0016 救护车 | 1.7 m；被拒后回固定翼版本 0.94 m |

## 地面站与签名

地面站任务包在真实/飞行/HIL 运行时要求签名。Ed25519 签名必须使用 **MSVC 版 OpenSSL**；MSYS2 的 MinGW OpenSSL 不能替代。便携包随带 `libcrypto-3-x64.dll`，部署时要与地面站一起保留。构建时可通过 `-OpenSSLRoot` 或 `MULTIDETECT_OPENSSL_ROOT` 指向 MSVC OpenSSL 目录。签名自测通过才可把该构建用于签名任务上传。

## Orin 上板一键命令

拿到 Orin 的 IP 和用户名后，在 Windows 开发机执行：

```powershell
$env:PYTHONPATH="src;."; python scripts/bringup_orin.py --host <板子IP> --user <用户名> --key $HOME/.ssh/id_ed25519
```

这条命令会刷新上板包、用 `tar | ssh` 传到板上、安装并运行 benchmark，再把结果收回 `artifacts/orin/<UTC时间>/`。只支持 SSH 公钥，不用密码；开发机公钥必须已经写入板子的 `~/.ssh/authorized_keys`。安装脚本用 `sudo -n`，所以板上该用户必须能免密 sudo，否则安装会直接失败。先检查命令、不连接板子时加 `--dry-run`。

## 已知限制与需要所有者的验收

- 以上固定翼、eVTOL 和定位数字来自 PX4/Gazebo SITL；HIL、真实飞行、真实相机视角和真实降落区仍需所有者验收。
- 尾座式悬停相机抬高 20°，30 m 高度主要看到约 75 m 以外的地面；当前适合先远后近，云台或下视相机才能根本改善。
- 在不锁屏、没人操作的桌面上，于主仓库运行联合 PX4/Gazebo/QGC 验收：

```powershell
python scripts/run_release_pre9.py --only-stage combined_px4_gazebo_qgc_acceptance --run-dir artifacts/release-pre9-runs/20261020-final
```

- 联合验收通过后仍要做 HIL/实飞和 QGC 人工目视检查；Orin 要按上一节的一键命令实测。仿真通过不等于可以跳过飞手的起飞、链路、返航和降落检查。

## 任务包：逐项填写

任务包是起飞前的边界和计划。所有地点使用名字，不填写原始经纬度；例如 `poi:gate`、`zone:field`、`route:fence`、`home`。上传后先做校验，校验失败不得起飞。

1. 填 `mission_id`，再填一句 `task_text`，或填经过校验的 `plan`。
2. 在 `places` 写点位，在 `zones` 写区域中心和半径，在 `routes` 写航路点；给它们起清楚的名字。区域可写 `risk_tag`，人员等高风险区域写 `oversight_required: true`。
3. 在 `operating_area` 写北、东方向的最小/最大范围和 `min_altitude_m`、`max_altitude_m`。每个步骤的高度必须在这个范围内。
4. 写 `budget`：总 `time_s`、总 `energy_wh`，以及返航保留的 `reserve_time_s`、`reserve_energy_wh`。总量必须大于保留量；任何一项到保留线，监督器会升级到返航。
5. 写 `home`，它必须位于 operating area 内。
6. 选 `risk_level`：`low`、`medium` 或 `high`。它只提供建议，不会替你改策略。
7. 明确写 `link_loss_policy`。可选项如下：

| 策略 | 失联后的动作 | 适合情况 | 风险等级建议 |
|---|---|---|---|
| `continue` | 继续执行 | 开阔、低风险、任务很短且现场允许无人看管 | `low` 默认 |
| `continue_then_return` + `timeout_s` | 先继续，静默达到时间后返航 | 低至中风险，想给链路短暂恢复机会 | `medium` 默认 |
| `hold_then_return` + `timeout_s` | 先悬停等待，达到时间后返航 | 目标需要等待确认，但不能继续向前 | 中风险或有人员区域 |
| `return_now` | 判定失联后返航 | 高风险、靠近障碍/人员或不应继续搜索 | `high` 默认 |
| `land_now` | 判定失联后请求降落 | 已确认下面是可安全降落区，且必须尽快落地 | 只在现场明确批准时用 |

`heartbeat_timeout_s` 是判定地面心跳静默的时间，默认 5 秒；它不是 `timeout_s` 的替代品。监督器还会因为电量/时间保留量、连续低置信度、需要地面监督的步骤或高风险区域而升级。飞机只能变得更保守：例如从继续变为悬停、返航或降落，不能在这些触发后自行放宽策略。

8. `oversight_steps` 列出必须有地面链路的步骤 ID。最后在 `plan` 中用 13 个技能串成步骤；每步有 `on_success` 和 `on_fail`，计划必须以返航、降落或最终悬停结束。`query` 最多 8 个词。

## 起飞前

### 从一句话到机上计划

地面站可以上传只包含 `task_text` 的任务包。机上会重新编译并回传 `plan`，操作员先检查步骤、区域和返航路径；若计划要求批准，必须发送“批准计划”，且摘要必须与机上 `plan_sha256` 完全一致。批准前飞机保持未起飞，拒绝或摘要不匹配不会启动任务。无通信预期的任务可在包中关闭 `requires_approval`，但仍会执行机上边界校验。

- 在地面站加载任务包并运行校验；确认点、区域、路线引用都能找到，计划终点是返航/降落/悬停，且高度、区域和预算合法。
- 检查电池、电量计、返航所需余量和 home 点；预留量不能被任务消耗。
- 按任务包策略核对 PX4 failsafe 参数。`continue`/定时策略通常由机上监督器管理，PX4 不应另行抢先执行相反动作；`return_now` 可用 PX4 Return 作同方向后备，`land_now` 只能在降落区确认后设置。PX4 的 `COM_DL_LOSS_T` 只是飞控超时，不等同于任务包计时。
- 检查相机画面、焦点、曝光、时间戳和存储卡；确认目标在允许高度能看清。
- Orin Nano 选择已批准的电源模式，确认散热和供电稳定；当前没有 Orin 实测基准。
- 确认日志盘有空间。每个任务目录会写 `mission.jsonl`、`summary.json`，并可能写 `downlink-cursor.json`。

## 飞行中

地面站应显示任务 ID、当前步骤和状态、连接/心跳、位置与高度、剩余时间/能量及保留量、监督器动作和原因、发现/计数/证据帧 ID/告警。报告只采用实际观测，不把模型的一句文字直接当作事实。状态中的 `count.lower_bound=true` 表示搜索未证明完整，不能当作精确总数。最终摘要可能分多页到达，显示“摘要不完整”前不要引用其中的部分结果。

操作员可发出暂停、继续和中止。暂停时监督器保持悬停；按策略达到暂停上限会返航。中止会进入安全恢复（返航或降落）。链路丢失时按任务包策略行动；重新连线可恢复等待中的 `hold_then_return`，不会把已经触发的更保守动作改回继续。

## 降落后

在任务日志目录查看 `mission.jsonl`：每行一个有递增序号的事件，如决策、执行、感知结果和最终摘要。`summary.json` 是最后一条 `final_summary` 的原子保存版本；`downlink-cursor.json` 只表示地面站已经确认收到的日志位置。

先看 `summary.status`、`action`、`reason`，再看 `report`、`findings`、`counts`、`history` 和 `recovery_history`。计数要结合证据帧 ID 和告警复核；链路中断期间的记录仍在本地，恢复后会按序补发。

## 常见故障

| 现象 | 先查什么 | 处理 |
|---|---|---|
| 上传即提示缺字段/未知字段 | JSON 拼写、必填字段 | 对照任务包字段；不要删掉 `oversight_steps`、`budget` 或策略。 |
| 高度超出或区域越界 | operating area 与步骤参数 | 降低/调整步骤高度或扩大已批准区域，重新校验。 |
| 计划无法结束 | `on_success`/`on_fail` 引用 | 确保存在 `return_home`、`land` 或最终 `hover`，且引用的步骤 ID 存在。 |
| 失联后动作比预期更保守 | `reason`、电量/时间余量、`oversight_steps`、高风险 zone | 这是安全升级；恢复链路后按现场决定是否结束任务，不要强行降级。 |
| 计数偏少/偏多 | 证据帧、目标大小、相似物体、光照 | 暂停或返航，人工复核；不要把一次计数当作精确测量。 |
| 日志无法打开 | 文件尾部、磁盘空间、mission_id | 保留原文件，不要截断；先复制出来检查最后一行和空间。 |
| Orin 推理慢、温度高或重启 | 电源模式、散热、空间 | 结束任务并返航；当前 Orin 性能尚未实测。 |

## 可直接试用的示例

`configs/onboard/mission-package-count-cones-example.json` 对应任务“数一数场地里有几个交通锥，然后返航”，使用 `zone:field`、15 m 高度、低风险 `continue`，无论计数成功还是失败都会执行 `return_home`。

用仓库的校验器验证：

```powershell
python -c "from pathlib import Path; from src.multidetect.autonomy.mission_package import load_mission_package; p=load_mission_package(Path('configs/onboard/mission-package-count-cones-example.json')); print(p.mission_id, 'VALID')"
```

校验通过只表示任务包结构和边界合法，不表示目标一定能识别，也不表示可以跳过飞手的起飞检查。

## 地面站任务监控卡片

飞行视图右侧会常驻显示可折叠的“任务监控”卡片。卡片展示任务状态、当前步骤（技能和查询）、进度、原语/预算、剩余时间和能量、计数与发现/告警总数，以及最近一次身份判定。计数带 `lower_bound` 时显示“至少 N”，表示仍可能有未覆盖区域。

链路栏显示当前连接状态、静默倒计时和到时将执行的失联策略。暂停、恢复和中止按钮直接发送经过现有签名操作员通道的指令；按钮反馈以机载确认和后续状态为准。

计划包含 `requires_approval` 时，地面站弹出计划预览，列出技能、参数和区域。确认步骤与区域无误后点击批准；拒绝会将 `reject_plan` 与任务编号及计划摘要发送给机载端。审批窗口关闭不等于批准。

地图会自动绘制计划区域、路线以及机载上报的发现和告警。发现标记包含标签、置信度和 sigma 误差圆；本地北/东坐标优先使用计划中的 WGS84 原点，没有原点时回退到飞机 HOME，不需要手工输入经纬度。

任务结束后，监控卡片显示分页重组的完整摘要，包括计数下限说明、发现及证据帧编号和步骤历史。摘要页缺失时只显示“摘要不完整，等待重传”，不会把不完整内容当作最终结果。
