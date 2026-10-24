# 机载进程崩溃恢复

每个已完成的监督器控制步可写入 `crash_resume/checkpoint.json`。文件通过临时文件、`fsync`、`os.replace` 原子替换，最大 64 KiB。写入失败只记录 `checkpoint_error`，控制循环继续由既有安全层负责。

检查点格式为 `multidetect.crash-resume.v1`，包含 `mission_id`、`package_digest`（准入任务及计划摘要）、`step_id`、`phase`、单调递增 `sequence`、`saved_at`，以及 `counts`、带位置的 `alerts`、`visited`、`home`、`elapsed_s`、`energy` 和 `provenance`。恢复时身份、时间和序号均须有效；截断 JSON、任务包摘要不符和过期记录都不可信。告警与计数在恢复时回灌执行器，位置记录继续用于空间去重。

## 启动决策

| 车辆状态 | 检查点 | 决策 | 中文审计原因 |
|---|---|---|---|
| 地面、未解锁 | 任意（旧文件保留） | 新任务 | 飞机在地面，按正常新任务流程启动 |
| 空中 | 有效且 mission_id/摘要一致、未过期 | 恢复 | 飞机在空中且检查点有效，继续执行；不得 arm/takeoff |
| 空中 | 缺失、损坏、序号回退、摘要不符或过期 | RTL | 无法证明任务状态，返航 |
| 空中 | 同上，且机型无 RTL | 降落 | 按机型 recovery policy 降落 |
| 状态未知 | 任意 | RTL/降落 | 状态未知，无法安全恢复 |

每个分支写入 mission log，结束时写 `final_summary`。旧检查点只归档，不删除。

## SITL 验证

先启动已准入的 PX4 SITL 任务并确认 `mission_log` 与检查点路径。飞行进入第二个步骤后，记录 onboard 进程 PID，仅结束该 PID（例如 Linux `kill -TERM <pid>`，不要按进程名批量结束）。等待 systemd 拉起同一服务，确认日志出现 `crash_resume`，步骤、计数和告警位置延续，且 MAVLink 日志中没有新的 arm/takeoff；随后检查点损坏、替换为不同摘要和设置旧 `saved_at`，应分别记录中文原因并 RTL（无 RTL 时按机型策略降落）。本仓库只提供离线 harness，未实际启动 SITL。
