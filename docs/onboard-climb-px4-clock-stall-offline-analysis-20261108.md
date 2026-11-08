# v4 climb 故障离线时间线分析（2026-11-08）

## 范围与方法

使用新增的 `scripts/analyze_px4_clock_stall_timeline.py` 对 v4 故障 episode 做只读分析。输入为 `steps.jsonl`、`mavlink.jsonl`、`host_pressure.jsonl`、`px4-clock-stall-capture.json`，并读取 `episode.json` 中 episode 创建时的墙钟/单调钟锚点。脚本不依赖 PX4、Gazebo、Docker 或 GPU，不写 episode，也不修改采集器、安全策略或 timeout。

执行命令：

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/analyze_px4_clock_stall_timeline.py \
  /data/projects/Multi-Detect-env/runs/src-followup2/.tmp/jobs/climb-fault-sitl4-1169782/artifacts/onboard-climb-fault-sitl4-20261108T002732Z-1169782/operator-abort/campaign/gen_cnt2_w0003/episodes/episode-0000-540aee44 \
  --json
```

单调钟与墙钟偏移由 `episode.created_wall_time - episode.created_monotonic_ns` 得到。PX4 时间来自 MAVLink `LOCAL_POSITION_NED.time_boot_ms` 与 steps observation 的 `pose_time_boot_ms`；Gazebo 时间来自 capture 中的 `/world/default/clock` 探测。capture 本身没有 monotonic timestamp，故实际 capture monotonic 时间由墙钟锚点换算；独立的 watchdog 截止 monotonic 时间由最后 PX4 进展时刻加 45 秒推导。报告保留这种来源标记，不把换算值伪装成采集器原始读数。

## 计算结果

| 事件/指标 | 结果 | 解释 |
| --- | --- | --- |
| climb fault step 1 | `return_home_climb_timeout`，outcome `timeout` | 输入要求向上 15 m；这是本 episode 唯一带此 fault 注入标记的 climb outcome。 |
| climb 的 PX4 elapsed | 39.436 → 59.632 s，共 20.196 s | 来自 climb 前后 observation 的 PX4 boot 时间。 |
| climb 的 primitive wait | 95.33465 s | 使用 `timing.command_and_primitive_wait_ms`，不是 step `receive_monotonic_ns`。 |
| climb RTF | 0.21184 | `20.196 / 95.33465`。表示该记录窗口内 PX4 时间推进约为墙钟的 21.2%。 |
| MAVLink 最后位置进展 | PX4 69.672 s；单调 125490.3969 s；UTC 2026-11-08 00:35:23.252 | MAVLink 最后一条 `LOCAL_POSITION_NED` 在 125490.3916 s 收到，capture diagnostics 的最后进展值约晚 5.3 ms。 |
| 45 s watchdog 截止 | 单调 125535.3969 s；UTC 00:36:08.252 | 由最后 PX4 进展 + 45 s 推导。它早于 climb timeout 的预计时刻约 3 秒。 |
| climb timeout 预计时刻 | UTC 约 00:36:11 | 由 step `obs_before.receive_monotonic_ns` 加 `command_and_primitive_wait_ms` 推导；不是 outcome 自带的结束 timestamp。 |
| capture `deadline` | UTC 00:36:12.087；Gazebo 81.484 s | 采样实际发生在推导的 watchdog 截止后约 3.8 s。 |
| capture `after_3s` | UTC 00:36:31.262；Gazebo 84.284 s | 两次样本间 Gazebo clock 增加 2.800 s，约 0.146 RTF（按两次 capture 墙钟间隔计算）。 |
| PX4 listener 时间戳 | IMU 69.672 s、vehicle status 69.488 s，两次探测均未变化 | 是有限样本探测结果；capture 样本 JSON 没有直接存 listener timestamp 数值，本报告不把它填入样本字段。 |

记录顺序不是“climb timeout → PX4 最后进展 → watchdog”。最后 MAVLink 位置进展发生在约 00:35:23，早于 climb timeout 约 48 秒；watchdog 截止约 00:36:08，又早于估算的 primitive wait 结束约 3 秒。climb timeout 时间是依照 wait duration 重建的近似时刻，不能作为原始精确事件时间。PX4 boot time 从 59.632 s 后继续推进至 69.672 s 的证据与此相符：primitive 的 `obs_after` 不是最后一次 PX4 clock 更新。

Host pressure 共 82 个样本，覆盖 UTC 00:30:01.940 至约 00:36:47.461。load1 中位数 233.03、95 分位数 355.53、峰值 366.78；cpuset core busy mean 字段的样本中位数 97.36%、95 分位数 99.46%、峰值 99.84%（81 个非空值）；CPU PSI `some avg10` 中位数 25.065%、95 分位数 53.54%、峰值 55.53%。这是宿主压力的同期背景，不足以单独归因 stall。capture 两个采样点报告 cgroup 未见 CPU throttling 增长、OOM 计数为 0；没有故障窗口内连续 `cpu.stat`/`memory.events` 时间序列。

## 证据边界

- capture 的 Gazebo runner 栈两次均分类为 `step`；未观察到 `PublishStats` mutex wait。它不识别 mutex owner，也不能证明 Gazebo 没有阻塞。
- `gz_world_paused_samples` 为 `[null, null]`，world paused 状态未知。不能据此声称 world unpaused。
- capture 明确将 camera progress 标为 `unknown`，因为该采集器没有探测 camera topic。
- capture 只对 listener 做有限前后采样。PX4 listener 时间戳不变且 Gazebo clock 有推进，支持两时钟域在这段探测窗口内分离；无法描述探测间的连续行为或确定根因。
- Host load/PSI 与近满载 core busy 支持存在重负载背景，但不能证明它是唯一或直接根因。有限 cgroup 快照不能排除此前短暂调度饥饿。
- 没有证据证明发生第二次注入 climb/第二次 timeout 命中，也没有据此推断 LAND、着陆或 disarm。

## 验证

```text
.venv/bin/python -m pytest tests/test_analyze_px4_clock_stall_timeline.py -q -p no:xdist
4 passed

.venv/bin/python -m ruff check scripts/analyze_px4_clock_stall_timeline.py tests/test_analyze_px4_clock_stall_timeline.py
All checks passed

python scripts/run_onboard_gate.py -q -p no:warnings
onboard gate: PASS (130s)
```

本次只新增离线分析脚本、针对性测试和本文档。未启动 SITL、Docker、GPU 任务或网络下载，未写入 `artifacts/`。
