# SIM 操作员端到端指南

本文对应 `wt/integration` 的 `4feb048`：从 Gazebo/PX4 会话、操作员 prompt，到机载规划、审批、飞行和结果下行。它描述的是 SIM 操作，不是实飞安全流程。

## 系统边界

```text
相机/深度 -> 快模型（候选框） -> 慢模型（身份判断）
                                      |
操作员 prompt -> GCS -> operator-link -> 机载 planner -> MissionSupervisor -> PX4/Gazebo
                                      |                         |
                                      +<-- plan/status/findings/final_summary --+
```

快模型逐帧找候选；慢模型按任务判断身份，并在任务开始时把 prompt-only 包编译成计划。`MissionSupervisor` 负责审批门、链路状态、预算/能量和返航策略；PX4 SITL 执行飞行，Gazebo 提供世界和相机。GCS 通过签名 MAVLink2 `TUNNEL` 承载 operator-link，并展示计划、状态和结果。

## 启动会话

先确认 Docker Desktop、Python、PX4/Gazebo 镜像已就绪；本地 worktree 必须有 `artifacts` junction，提供 Fuel 离线缓存和 onboard 所需的 `llama-server`：

```powershell
$env:PYTHONPATH="src;."
python scripts/run_sim_session.py --world gen_cnt2_w0000 --gcs-port 14560 `
  --snapshot-dir D:/Projects/mdwt/_scratch/e2e/shots `
  --session-log D:/Projects/mdwt/_scratch/e2e/session.jsonl
```

脚本的 `--world` 接受 `simulation/gazebo/worlds` 下的 SDF 名称或仓库内路径；`--airframe` 默认 `x500_mono_cam`，也可使用已有 profile（例如 `standard_vtol`、`rc_cessna_runway`）。PX4 会主动向宿主 `127.0.0.1:14560` 发送 GCS MAVLink；该端口不通过 Docker publish。浏览器打开 `http://127.0.0.1:6080/vnc.html?autoconnect=1`（`--viewer-port` 可改端口），手动控制端口仍是 `127.0.0.1:14540`。

首次使用或 viewer 镜像缺失时加一次 `--build-viewer`；后续会复用缓存。`--snapshot-dir` 配合 `--snapshot-interval`（默认 10 秒）保存 `DISPLAY=:99` 截图，世界 ready 和任务结束还会各保存一张。`--session-log` 写 JSON Lines 生命周期日志。`--check-heartbeat S` 会在宿主 GCS UDP 端口等待 HEARTBEAT 并写 `heartbeat.json`。

`--onboard-task "..."` 运行直接由会话启动的 onboard policy-chain smoke，输出在 `--onboard-out`（默认 `tmp/sim-session/onboard`）；`--exit-after-task` 在任务成功或失败后清理容器并返回任务 exit code；`--task-timeout-s` 默认 2400 秒。要走完整 prompt-to-flight operator-link，使用 `--operator-link`，并让 GCS 连接默认 `--operator-port 14580`。会话结束前会保持 SIM 电量，且等待 operator downlink 排空；这两个行为来自当前分支最新修复。

所有 Docker 操作都要持有共享 SITL 锁（Git Bash）：

```bash
. /d/Projects/mdwt/_scratch/sitl_lock.sh
sitl_acquire simgui
python scripts/run_sim_session.py --operator-link --world gen_cnt2_w0000
status=$?
sitl_release
exit $status
```

## 发送 prompt

prompt-only 包只需要任务文本和任务边界，不预置 `plan`。例如 `D:/Projects/mdwt/_scratch/e2e/package-count-cones.json` 的核心字段如下（完整文件可直接使用）：

```json
{
  "mission_id": "e2e-count-cones-001",
  "task_text": "数一数场地里有几个橙色交通锥，报告位置后返航",
  "risk_level": "low",
  "heartbeat_timeout_s": 5,
  "home": [0, 0],
  "zones": {"zone:site": {"centre_ne": [0, 0], "radius_m": 55}},
  "operating_area": {"north_min_m": -100, "north_max_m": 100, "east_min_m": -100, "east_max_m": 100, "min_altitude_m": 5, "max_altitude_m": 120},
  "budget": {"time_s": 3600, "reserve_time_s": 180, "energy_wh": 600, "reserve_energy_wh": 60}
}
```

会话打印出的 `operator-link.json` 给出端口和密钥。另开终端运行：

```powershell
python scripts/fake_gcs_console.py D:/Projects/mdwt/_scratch/e2e/package-count-cones.json `
  --link tmp/sim-session/onboard/operator-link.json `
  --auto-approve --log D:/Projects/mdwt/_scratch/e2e/gcs.jsonl --max-seconds 2400
```

省略 `--auto-approve` 时，收到 plan 会停在 `approve with Enter`；`--max-seconds 0`（默认）持续到 Ctrl-C。控制台会打印上传结果、type-29 onboard ACK、计划、约每秒一次的 status v2、每条 finding，以及拆页后的 `final_summary_page`；只有收齐所有页才是最终摘要。`--log` 追加每份完整 JSON 文档，并记录 `received_at_wall`、`received_at_monotonic`、首片接收时间和 `completed_at_wall/monotonic`。

自写 GCS 必须满足这些协议义务：

- 建立 operator codec 时注册真实视频流几何；当前示例是 `camera-main`、1280×720，宽高和旋转必须与状态一致。
- 每秒发送 type-34 `MISSION_HEARTBEAT`，否则机载链路变为 down，下行日志会停止。
- 收到每个 type-35 telemetry chunk 后立即回 type-36 ACK（token、chunk index）；下行是 stop-and-wait，机载会重发未确认分片。
- type-28 上传按 20 burst、20/s 的消息类型限制分发；每片间隔至少约 0.06 秒，不能一次打满。
- 每次新上传生成新的奇数 upload token；复用旧 token 会被判为 expired or conflicting。收到 type-29 逐片 ACK，最后一片成功才算装载完成。

完整消息表和分片边界见 [GCS 任务包流程与 TUNNEL v2](gcs-mission-package-workflow.md)。

## 真实感选项

从源 SDF 生成不改源文件的变体：

```powershell
python scripts/augment_sim_world.py simulation/gazebo/worlds/gen_cnt2_w0000.sdf `
  --wind-mps 6 --gust-mps 3 --gust-period 4 --time-of-day dusk `
  --actors 2 --terrain procedural --camera-noise 0.01
```

可选项是 `--wind-mps`、`--gust-mps`/`--gust-period`、`--time-of-day dawn|noon|dusk`、`--actors N`、`--terrain dem|procedural`、`--camera-noise [STDDEV]`、`--fog` 和 `--register-index`。风只写 world `<wind>` 与阵风元数据；容器启动时依据 server config 注入 `WindEffects`，不能把 XML 存在当作动力学证据。相机噪声只修改世界中明确存在的 camera/depth/segmentation sensor；PX4 `mono_cam` 需要相应 model overlay。演员使用本地 mesh，不依赖 Fuel。具体限制和验证方法见 [仿真真实感变体](sim-realism.md)。

## 移动目标跟随回归

仓库内置的 `configs/scenarios/generated-movers` 已包含由仿真时间驱动的移动目标。`gen_mov_w0000` 是穿红衣服的人，真值文件提供闭环航迹；SDF 中的目标模型由 `scripts/gz_mover_driver.py` 按轨迹更新，因此不需要另造 world 或启动 SITL 才能做套件接线检查。

跟随任务套件为 `configs/onboard/suite-follow.json`，任务要求计划技能严格为 `[follow, return_home]`。计分器从 `steps.jsonl` 的仿真时间戳与目标真值轨迹配对，输出目标在画面内比例、平均/最大水平跟随距离、跟丢次数、每次及最大重新获取时间、误跟对象次数，并要求默认目标在框率和时间覆盖率至少 0.8、平均距离不超过 10 m、最大距离不超过 20 m、最多丢失 3 次、最大重获时间不超过 5 s、误跟为 0。套件可用 `expect` 中的 `min_`/`max_` 字段收紧这些门槛。

先做不启动仿真的 dry-run：

```powershell
$env:PYTHONPATH = 'src;.'
$env:PYTHONUTF8 = '1'
python scripts/run_mission_suite.py `
  --suite configs/onboard/suite-follow.json `
  --out D:\Projects\mdwt\_scratch\cx-follow-suite\dry-run `
  --dry-run
```

实际 SITL（需要已配置 PX4/Gazebo、模型和磁盘空间）命令：

```powershell
$env:PYTHONPATH = 'src;.'
$env:PYTHONUTF8 = '1'
python scripts/run_mission_suite.py `
  --suite configs/onboard/suite-follow.json `
  --out D:\Projects\mdwt\_scratch\cx-follow-suite\run `
  --strict
```

计分器也可对单个 episode 直接运行；`--truth` 指向相应 `gen_mov_w0000.truth.json`。不启动 SITL 的单元覆盖见 `tests/test_score_onboard_loop.py`。

## 故障排查

| 现象 | 原因/处理 |
|---|---|
| 14560 无 HEARTBEAT | PX4 已改为主动发往 `host.docker.internal`；确认 GCS 绑定 `0.0.0.0:14560`、未被占用，并持有 SITL 锁。 |
| noVNC 404 或空白 | 首次加 `--build-viewer`，看 `docker logs`；确认浏览器访问 `/vnc.html?autoconnect=1`。 |
| 一直 Waiting for Gazebo world | 检查 Fuel/llama-server 的 `artifacts` junction；风世界不能在 SDF 声明插件，WindEffects 由 server config 注入。 |
| Duplicate mount point `x500_mono_cam` | 分割相机和风 overlay 不能同时挂同一目标；使用合并的 `x500_mono_cam_seg_wind`。 |
| 风一开就滑行、翻倒或无 heading reference | 当前实现把风逐步 ramp 到目标值并降低作用系数；用短 SITL 姿态/漂移观测确认，不以 SDF 代替实测。 |
| 上传 ACK 丢失、`expired or conflicting` | 上传超过 20 burst/20/s，或复用了 token；按约 0.06 秒 pacing，每次运行生成 fresh token。 |
| 有 plan 但没有 status/finding | GCS 没发 type-34，或未 ACK type-35；保持 heartbeat、逐片回 type-36。 |
| 任务提前返航 | 读取 session log 和 status 的 supervisor reason；历史回归曾因时间线缺飞机位置触发错误能量返航，当前 SIM 仍在修 energy reserve/battery-stale early return。 |
| `battery telemetry stale` 但电量充足 | 先查 `mavlink.jsonl` 的 `time_boot_ms` 是否断档、宿主机进程是否卡住（第九次演示：Windows 上机载进程整体停约 20 秒，UDP 缓冲溢出丢了 PX4 时钟 354.0–361.1 秒的全部遥测，PX4 报 `Failsafe activated`）。SIM 读取器把这类“整条链路无消息”的车辆时钟区间记为 `vehicle_telemetry_gaps_s`，能量模型不把它计入电池时效，并在任务日志写 `vehicle_telemetry_gap`；链路恢复后电池仍需在 5 秒内上报，否则照常返航。实机仍按宿主接收时间判定。 |
| 最终摘要只到 page 1 | 旧版所有页共用一个传输 token、分片序号跨页累加，GCS 按分片头 ACK 第 2 页时机载不认，page 1 被反复重发。现在每页是独立传输（各自 token、序号从 0 开始）。 |
| 任务接纳前心跳被拒 | 机载尚无任务时收到 type-34 心跳不回 ACK、不计入任务心跳；已有任务而任务标识不符时仍回拒绝。 |
| 演示计数远少于回归套件（同一世界同一配置） | 第十次演示 gen_cnt2_w0000 只数到 1/3（count `budget_exhausted`，140 步里 62 步在 55 m 场地外，最远 90.8 m），套件 3 架次均 3/3。三处与套件不一致：① 会话采集器未传 `--rgb-size`，用了 640x480（套件与相机标定为 1280x960），锥桶框只有 6–9 px，低于 10 px 的身份判断下限，另两个锥桶被跟踪多次却从未送慢系统（慢调用 24 次/match 4 次，套件 42–56/21–26）；② operator-link 规划只给了不带标签的 `zone:site`，中文“场地”对不上，规划成 `zone:all`，搜索半径退回 150 m（离线复现：r18 与 r14 带上 `场地 / the site` 标签都选 `zone:site`）；③ 策略任务文本停在占位句 `operator link mission`，按 2 m 默认物体定搜索高度（13.0 m、24 m 环距，套件 11.3 m、20 m），摘要标题也是占位句。现在会话传 1280x960，operator-link 规划沿用包内 `ref_labels`，安装包时按包任务与 query 重算搜索高度和任务文本。operator-link 规划仍用身份模型 r18，而 from_text 用 `planning_model` r14。 |
| 回归架次显示 missing | 先看磁盘门限和 `campaign/*.log`；10-22 relook 回归还遇到缺少 `artifacts/experiments/yoloe-v3-onboard-1280/train/weights/best.pt`，没有产生可评分 episode。 |

## 端到端演示记录（2026-10-22/23）

用 `scripts/run_e2e_operator_acceptance.py --evaluate <运行目录>` 评分（判据见 [e2e-operator-acceptance.md](e2e-operator-acceptance.md)）。

| 运行 | 提交 | 结果 | 主要问题 → 修复 |
|---|---|---|---|
| run9 | a21ead8 | FAIL：计数 2，摘要只到第 1/2 页，电池误判提前返航 | 摘要分页共用 token → 每页独立传输；机载进程卡顿造成的链路断档不再计入电池年龄 |
| run10 | 9125589 | FAIL：计数 1（budget_exhausted） | 会话漏传 `--rgb-size 1280x960`、包规划没带 ref_labels（规划成 zone:all）、任务文本停在占位句 |
| run11 | 0fb2155 | **PASS**：计划签名审批 0.34 s；3 个 finding 到地面延迟 ≤0.8 s；完整两页摘要；coverage_complete 计数 3/3，位置误差 0.08–0.29 m；返航末帧离家 0.26 m | — |
| run12 | 5cebc74 | **PASS**：计数 3/3，误报 0，`count_is_lower_bound=false`（区域外/已补看地点不再算未决）；机载 llama-server 关闭主机提示缓存后，可用内存中位数 858 MB（run10 为 637 MB），无 vehicle_telemetry_gap；启动阶段 1–2 分钟内可用内存仍会降到几十 MB（加载模型与 Gazebo，同时其他项目的 java、WSL 虚拟机占用大量内存） | — |
| run13 | ed3cbe7 | **PASS**：加入计划意图一致性检查（返航意图、查询目标依据、地点依据）后复验，7 项判据全部通过 | — |

## 批量操作员 E2E 回归

`configs/onboard/suite-e2e-operator.json` 把五条地面站 prompt 流程定义为一组回归：数交通锥、数消防栓、监视背包两分钟、沿围栏巡逻找背包、四向检查邮筒。每项指定世界、机型、任务包边界和预期技能/判据；控制台会自动签名并审批机载计划。每个任务的会话、下行记录和 `operator-acceptance` 报告分别放在 scratch 输出目录，套件结束后生成 `suite-results.json` 与 `suite-results.md`。

先做纯命令预览，不取 SITL 锁，也不启动 Docker：

```powershell
$env:PYTHONPATH = 'src;.'
$env:PYTHONUTF8 = '1'
python scripts/run_e2e_operator_suite.py configs/onboard/suite-e2e-operator.json `
  --output D:\Projects\mdwt\_scratch\cx-e2e-multi\dry-run --dry-run
```

实际批量运行会串行获取共享 `D:\Projects\mdwt\_scratch\sitl.lock`，与 `sitl_lock.sh` 的 mkdir/owner 锁互斥。每个任务最长会话等待为 90 分钟，五项连续运行按 30–120 分钟预留（取决于世界启动、机载模型加载和仿真结束时间）；结果日志仍写入 `D:\Projects\mdwt\_scratch\cx-e2e-multi\`：

```powershell
$env:PYTHONPATH = 'src;.'
$env:PYTHONUTF8 = '1'
python scripts/run_e2e_operator_suite.py configs/onboard/suite-e2e-operator.json `
  --output D:\Projects\mdwt\_scratch\cx-e2e-multi\run
```

评估器按计划技能区分计数真值匹配、监视/巡逻告警精确率与召回率、巡检目标匹配，并保留返航、签名审批、下行完整性等公共门槛。历史 run9–run13 的计数判定与 verdict 维持原值。

## 已知限制

- operator-link 规划用身份模型（r18），from_text 用规划模型（r14）；本任务两者结果一致，其他任务未验证。
- GCS 审批按钮未和真实 SIM 会话联调；地面站联合验收由负责人执行。
- 单架次结果不能当作稳定性或实机保证；回归基线见 `sim-mission-suite-regression-20261020.md`。
