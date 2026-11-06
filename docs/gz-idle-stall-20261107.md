# Gazebo idle stall 调查（2026-11-07）

## 结论

28-head 当前只发现一次 PX4 clock stall：`count-cones/campaign/gen_cnt2_w0000/episodes/episode-0000-4dec0c9f`，第 6 步，PX4 clock 停在 49.116 s。该 episode 使用 logger off。此前约 70 个 logger-off episode 未复现。本次证据能确认 PX4 与 Gazebo 进程仍存活、PX4 clock 不再推进，但不能确认 Gazebo 世界是否暂停，也不能区分 Gazebo 在等待渲染/传感器/GPU、lockstep 链路停止或其他内部等待。没有足够证据修复某个根因。

## Episode 证据

- `episode.json` 的 `status=failed`、`failure_class=clock_stalled_alive`；pre-arm `ready=true`，启动于新 episode `episode_index=0`，不是 resume 重跑。provenance 记录 logger `off`、PX4 daemon mode、Gazebo world `gen_cnt2_w0000.sdf`，PX4 host PID 90315。
- `steps.jsonl` 显示 step 0–5 primitive 成功。step 6 是 `translate`（forward 13.28 m）；开始后超过 45 s 未见 primitive 完成，collector 抛出 `PX4 simulation clock stalled`。最后 MAVLink `time_boot_ms=49116`，接收时间约 2026-11-07 03:08:18 UTC，与冻结的 49.116 s 一致。
- `px4-clock-stall-capture.json` 两次采样相隔 15.93 s，`gz topic -e ... /world/default/clock` 均未取得消息，分析结果是 `gz_clock_advanced=unknown`，不是确认 clock 没动。`gz topic -i` 在高负载下运行 3 s 后报无 clock publisher，但同时列出 PX4 subscriber；该探针会注册 transport node，单独不足以判定服务端状态。
- 两次采样间 gz 有 129 个线程，`on_cpu_delta` 合计约 0.105337 s；MAVLink 最后一批 attitude、position 和 servo 消息停在 49.116 s。PX4 的 Gazebo transport reception 线程在 `poll`，与没有 clock 消息相符，但缺少 gz 端栈，因此不能判断 clock 发布端为何不发。
- stall capture 时 host load average 约 `[232, 239, 224]`，load/core 约 2.3–2.5；container snapshot 记录 GPU0 占用约 13.5 GiB。它们证明当时资源竞争背景存在，不证明其他 GPU0 工作导致 Gazebo 卡住。旧 capture 没有 world stats/pause state，也没有 gz gdb。

## 竞争解释

| 假设 | 现有证据 | 判断 |
| --- | --- | --- |
| world 被暂停或 stepping 停止 | `gz sim` 启动参数包含 `-r`；检查到的 collector 控制命令只向 `/world/default/wrench/persistent` 和 `/world/default/wrench/clear` 发布实体 wrench/clear，没有 world pause 或 step 命令。旧 capture 未读取 WorldStatistics。 | 未见本仓库显式 pause/step 命令；无法排除运行时外部暂停或 Gazebo 内部停止更新。 |
| rendering sensor / GPU 等待 | Gazebo 进程使用 `--headless-rendering --render-engine ogre2`；GPU0 同期有占用，gz 几乎无 CPU 增量。 | 合理候选，但进程在 GPU wait、渲染锁或其他等待上的证据缺失。 |
| frame server back-pressure | `LatestCandidate.put()` 只替换候选并通知；请求循环等待新 stamp 后才将 packet 写到 stdout。subscriber callbacks 不写 pipe、不等待 host reader。 | 没有证据支持它能反向阻塞 Gazebo physics。若 stdout 写阻塞，只会阻塞 frame server 请求线程；此次冻结发生于 step 6 的 primitive 等待。 |
| PX4 logger off | 日志确认 `SDLOG_BACKEND=0`；同类 logger-off episode 多次正常，本次也已完成起飞和 6 个 primitive。 | 不能据此认定 logger 设置是原因；未观察到关闭 logger 后直接影响 clock 的代码路径。 |
| pre-arm budget / resume 改动 | 本 episode pre-arm 已就绪，且 `episode_index=0`、无 resume；预算逻辑只影响 pre-arm health wait。resume 改动只挑选中断 episode 重新收集。 | 与本次 stall 路径不符。 |

## 提交范围核查

检查了 `cb8244a3..64bc76d4`。`64bc76d4` 本身只合入 r9t 分析文档。相关运行时代码提交中，logger 默认 off 改变启动环境及 ULog 收集策略；pre-arm 预算改为 Linux PX4 clock 预算并设 wall ceiling；stall capture 增加 transport 证据；resume 改动只影响 campaign 重跑选择。它们可能改变运行环境或 stall 后的观测，但现有证据没有把任一项连到此次 Gazebo clock 发布中断。r9r 提交更新场景和生成世界；此次实际世界是 `gen_cnt2_w0000.sdf`，不能仅凭提交时间排除世界负载的影响。

## 采集改进

`scripts/px4_clock_stall_capture.py` 现增加对 `/world/default/stats` 的只读、限时采样，并解析 `paused` 与 sim time。它还会像 PX4 一样对 `gz sim` 做前后两次 gdb 快照，输出到 `px4-clock-stall-gz-gdb.txt`；两进程共用既有 30 s gdb 总预算，降低每个快照超时以保持总预算。采集不向 world control service 发送请求，不会主动 unpause 或 step。pause 字段或 stats 消息未取得时会记录 unknown/缺失，而不推断状态。

## 28-head 后续状态

本报告收尾时扫描到 8 个 episode.json，其中只有上述 1 个 stall-like failure；其余已落盘 episode 没有 stall failure。gate 运行期间新增的 episode 也已纳入这次扫描。`artifacts/` 是共享只读产物，扫描仅做读取。

## 验证

- `tests/test_px4_clock_stall_capture.py`：14 passed。
- `python scripts/run_onboard_gate.py -q -p no:warnings`：PASS（pytest 通过，176 s）。
