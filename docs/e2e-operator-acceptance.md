# Operator-link E2E 验收评估器

`scripts/run_e2e_operator_acceptance.py` 对一次「地面站 prompt → 机载规划 → 签名审批 → 飞行 → 下行」SIM 架次打分。它有两种用法：

- `--evaluate RUN_DIR`：只读已有运行目录并出报告，不碰 Docker/SITL。
- `PACKAGE`（位置参数）：取共享 SITL 锁，启动 `run_sim_session.py --operator-link --exit-after-task`，等到 `onboard/operator-link.json` 出现后启动 `fake_gcs_console.py --auto-approve`，会话结束后清理进程/容器、释放锁，再对本次目录打分。

退出码：`0` 全部判据通过，`1` 有判据失败，`2` 运行出错（锁被占、没有 `operator-link.json` 等），`130` 被 Ctrl+C 中断。

## 用法

评估已有架次（报告默认写 `RUN_DIR/operator-acceptance.json|.md`，`--report` 可改路径前缀）：

```powershell
$env:PYTHONPATH = "src;."; $env:PYTHONUTF8 = "1"
python scripts/run_e2e_operator_acceptance.py --evaluate D:/Projects/mdwt/_scratch/e2e/run10 `
  --report D:/Projects/mdwt/_scratch/e2e-accept-eval/run10
```

跑一次新架次并评估（需要 Docker、`artifacts` junction，会先取共享 SITL 锁 `D:/Projects/mdwt/_scratch/sitl.lock`；`--lock-timeout` 秒内拿不到则退出 2，默认 0 表示一直等）：

```powershell
python scripts/run_e2e_operator_acceptance.py D:/Projects/mdwt/_scratch/e2e/package-count-cones.json `
  --world gen_cnt2_w0000 --airframe x500_mono_cam `
  --output D:/Projects/mdwt/_scratch/e2e/run11 --lock-timeout 600
```

其他参数：`--truth FILE` 指定真值 JSON（默认按 `episode.json` 的 `world_sdf` 走 `score_onboard_loop.truth_for_episode` 的查找顺序）；`--radius-m` 计数匹配半径，默认 10 m，与 `score_onboard_loop --radius-m` 默认一致；`--gcs-timeout`、`--link-timeout`、`--gcs-port`、`--lock-name` 用于运行模式。

## 读取的证据

| 文件 | 用途 |
|---|---|
| `gcs-documents.jsonl` | 地面按到达顺序收到的完整文档（缺失时退回解析 `gcs.out` 中的 JSON 行） |
| `gcs.out` | 控制台输出里的 `onboard ack kind=K index=I accepted=… 说明` 行与 `rejected packet` 行 |
| `onboard/collector/*/mission_log/mission.jsonl` | 机载任务日志：plan、plan_approved、approve_plan 地面事件、finding、escalation、final_summary、vehicle_telemetry_gap |
| `onboard/collector/episodes/*/episode.json`、`steps.jsonl` | 世界 id（找真值）、末帧位置与 landed |

## 输出字段

- `plan`：地面是否收到计划；`signed_command_accepted`（机载记录了 `approve_plan` 地面事件或 type-37 签名命令 ACK 被接受）；`approved`（`plan_approved.plan_sha256` 与计划一致）；`approval_latency_s`＝机载 plan 记录到 plan_approved 记录的时间差（含计划下行、操作员决定、签名命令上行）。
- `findings[]`：每个机载 finding 是否到达地面，以及到达延迟区间 `latency_lower_s`/`latency_upper_s`。控制台不给文档打接收时间戳；下行是单队列 stop-and-wait，status 只在没有在途传输时才生成（`t` 为机载时钟），所以 finding 的地面到达时刻落在「它之前收到的 status 的 `t`」与「它之后收到的第一个 status 的 `t`」之间。延迟都按机载时钟计。
- `final_summary`：`complete`、来源（`final_summary`＝控制台直接收到或已拼好；`final_summary_pages_assembled_here`＝评估器自己按页拼成）、`pages_total`、`pages_received`、重复页数、机载是否记录了摘要。只收到部分 `final_summary_page` 即为不完整。
- `termination`：`status`（`completed` / `ended_early` / `unknown`）、`action`、`reason`、来源（地面完整摘要 → 地面 escalation → 机载摘要），以及机载自己记录的 `onboard_status` 作交叉核对。
- `count`：报告计数（地面完整摘要 → 最后一条带 counts 的 status → 地面 finding 数）、`is_lower_bound`、count 步骤原因（如 `budget_exhausted`）、真值数、匹配数、误报数和位置误差。匹配直接调用 `score_onboard_loop._count_metrics`（一对一最近邻，半径内才算），输入是地面收到的 finding 全精度位置。
- `return_home`：摘要里 `return_home` 步骤是否 `succeeded/arrived`，另给末帧离家距离和 landed 作参考。
- `link`：拒绝 ACK 按（消息类型、说明、阶段）汇总，阶段以第一份下行文档为界——之前多为任务接纳前的心跳竞争，之后才算链路故障；`vehicle_telemetry_gap` 段；status 中 link=down 次数和最大 `link_silent_s`；机载记录了但地面没收到的关键文档（mission_started/plan/plan_approved/finding/escalation/final_summary）。
- `criteria` / `verdict`：`plan_signed_and_approved`、`all_findings_at_ground`、`final_summary_complete`、`mission_completed`、`returned_home`、`count_matches_truth`、`no_rejected_acks_after_admission` 全部为真才 `PASS`。

## run9 / run10 的结果

| 项 | run9 | run10 |
|---|---|---|
| 计划签名审批 | 是，0.593 s | 是，0.375 s |
| finding | 2 个，均到达，延迟上界 0.172 s / 1.062 s | 1 个，到达，延迟上界 0.875 s |
| 最终摘要 | 不完整：只收到 page 1/2（重复 29 次） | 完整，`final_summary` |
| 终止 | `ended_early`，`return_now`，battery telemetry stale; return while feasible（地面 escalation） | `completed` |
| 返航 | 摘要未到地面，无报告；末帧离家 1.94 m | `return_home` arrived |
| 计数 / 真值 | 2（status，下限）/ 3，匹配 2 | 1（下限，`budget_exhausted`）/ 3，匹配 1 |
| 链路 | 任务接纳前 2 个 type-34 心跳拒绝；final_summary（seq 1308）未到地面 | 无拒绝 ACK |
| 判定 | FAIL | FAIL（仅 `count_matches_truth`） |

两次的机载日志里都没有 `vehicle_telemetry_gap` 记录。

## 测试

```powershell
python -m pytest -q tests/test_e2e_operator_acceptance.py
```

`tests/fixtures/e2e_operator/run9|run10` 是两次真实架次的裁剪副本：保留全部非 status 地面文档及其前后相邻的 status、首尾 status、机载关键记录、最后一个 episode step，以及除「已接受的心跳 ACK」以外的控制台行。裁剪不改变除 status 统计（文档数、最大静默）以外的任何评估结果。本机存在 `D:/Projects/mdwt/_scratch/e2e/run9|run10` 时，同一组断言也会对完整目录再跑一遍。另有合成用例覆盖：乱序分页由评估器自行拼接、延迟区间计算、`vehicle_telemetry_gap`、接纳后的拒绝 ACK、finding 未到地面。该测试已加入 `tests/onboard_gate_files.py`，随 `scripts/run_onboard_gate.py` 运行。
