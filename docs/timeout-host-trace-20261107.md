# PX4 传感器 timeout 与整机 trace 对齐（2026-11-07）

本报告只读分析以下产物，没有启动 SITL、Docker 或训练，也没有改变仿真行为和 CPU 分配：

- trace：`/data/projects/Multi-Detect-env/runs/host-trace/20261107.jsonl`
- r9r episode：`artifacts/r21id-r9rb/episodes`
- 后续 nightly：`artifacts/benchmarks/nightly-linux-r21idr3-r24plan-29-head`

分析快照在 **2026-11-07 11:26:56 +08:00** 读取完成。trace 当时有 123 个约 30 秒样本，覆盖 **10:25:23–11:26:34 +08:00**；r9r 有 55 个 episode，其中 13 个与 trace 时间区间重叠。`29-head` 目录当时没有 `episode.json`，因此不能作为本次统计的 episode 对照组。

## Timeout 的墙钟对齐

脚本现在递归读取 episode 目录，也读取 `mavlink.jsonl.gz`，从每条 `STATUSTEXT` 的 `receive_wall_time` 对齐最近的整机 trace 样本。55 个已读 r9r episode 共记录 **77 条 Accel timeout、0 条 Gyro timeout、0 条 MAG timeout**，分布在 17 个 episode。与 trace 重叠的 13 个 episode 中，只有下面一条传感器 timeout：

| episode | STATUSTEXT 墙钟时间 | 最近 trace 样本 | CPU PSI some | load | top cgroups（按该样本记录顺序） |
|---|---|---:|---:|---:|---|
| `episode-0000-a921b212` | 11:18:00.727 | 11:18:02.876 | 56.31% | 267.77 | 见下方该样本的完整 15 项 |

该样本记录的 15 个 top cgroup（括号内为 CPU 百分比）：`system.slice/md-lane-cx-identity-r10-prep.service` (14.87)、`gaius.slice/run-p227803-i8351516.scope` (7.43)、`f1cfd.slice/f1st-trim-m2.service` (5.95)、`f1cfd.slice/f1st-trim-m1.service` (5.93)、`system.slice/sunshine-desktop.service` (5.18)、`system.slice/f1st-r02-b-mp.service` (4.00)、`system.slice/f1st-r02-a-mp.service` (3.99)、`gaius.slice/gaius-train-filefix4.scope` (2.80)、`system.slice/f1aero-loops-1793972824.scope` (2.29)、`system.slice/docker-257a4eee5e705fe20e515410afc05b285afa21c315965decf5d95b35638a5d32.scope` (2.10)、`system.slice/docker-ed829e4b4d25c91c13b850a767ee16e8714c69addb3a80ec6e7d8f754481f3cb.scope` (2.09)、`gaius.slice/run-p310260-i8794701.scope` (2.06)、`system.slice/docker-720e70ec7325384ae726d1d2a546211ffe92888be0a0e2112ba538b002be9003.scope` (2.02)、`system.slice/md-lane-cx-nightly-review-report.service` (1.86)、`f1codex.slice/f1codex-p1107p5_09_step_off3b3_release_bom_fixture-3940455-478.scope` (1.51)。

这条 Accel timeout 发生在 trace 结束前约 15 分钟，因而有真实的主机样本可查。其余 76 条 Accel timeout 都在 trace 起点之前，或在本次快照中尚未与 trace 重叠；没有可报告的 trace 样本，不能把它们归因给本机负载。

## 重叠窗口：burst 与 quiet

对每个重叠 episode 取其墙钟区间内的 trace 样本。将全 trace 的 CPU PSI `some` 第 75 百分位（本快照 **55.325%**）作为简单 burst 阈值；episode 内任一样本超过阈值即为 burst。统计按 episode 计数，不把 30 秒样本当成独立故障。

| episode | 窗口（+08:00） | timeout 数 | RTF | CPU PSI some 均值/峰值 | load 均值/峰值 | 分类 |
|---|---|---:|---:|---:|---:|---|
| `episode-0000-11892190` | 10:14:56–10:26:24 | 0 | 0.261 | 47.8/48.7 | 79.5/84.8 | quiet |
| `episode-0000-ed1591e0` | 10:18:40–10:29:41 | 0 | 0.270 | 47.9/49.1 | 89.2/103.9 | quiet |
| `episode-0000-ebba4f9b` | 10:21:30–10:39:18 | 0 | 0.145 | 52.9/73.2 | 95.8/142.1 | burst |
| `episode-0000-f10999bd` | 10:26:26–10:41:34 | 0 | 0.178 | 54.7/73.2 | 99.6/142.1 | burst |
| `episode-0000-cd7354a4` | 10:29:43–10:42:09 | 0 | 0.234 | 56.7/73.2 | 100.4/142.1 | burst |
| `episode-0000-f5b7e62b` | 10:39:21–10:59:04 | 0 | 0.131 | 49.9/68.2 | 95.8/130.7 | burst |
| `episode-0000-c8bd3b39` | 10:41:37–10:54:40 | 0 | 0.223 | 49.3/67.7 | 95.5/130.7 | burst |
| `episode-0000-a5a8bff2` | 10:42:11–10:55:03 | 0 | 0.228 | 48.6/67.7 | 96.2/130.7 | burst |
| `episode-0000-f7d67f07` | 10:54:43–11:07:39 | 0 | 0.232 | 45.9/48.6 | 101.9/138.0 | quiet |
| `episode-0000-494f1f33` | 10:55:05–11:08:15 | 0 | 0.227 | 45.9/48.6 | 100.5/138.0 | quiet |
| `episode-0000-a921b212` | 10:59:08–11:22:00 | 1 | 0.104 | 49.4/60.6 | 160.2/359.4 | burst |
| `episode-0000-edd66c38` | 11:07:42–11:21:31 | 0 | 0.205 | 51.4/60.6 | 186.6/359.4 | burst |
| `episode-0000-6a92249a` | 11:08:18–11:22:03 | 0 | 0.212 | 51.9/60.6 | 201.1/359.4 | burst |

汇总如下：

- burst：9 个 episode，1 个有 timeout、8 个没有；CPU PSI some 均值的 episode 平均为 51.7%，load 均值的 episode 平均为 125.7，RTF 中位数 0.205。
- quiet：4 个 episode，0 个有 timeout、4 个没有；CPU PSI some 为 46.9%，load 为 92.8，RTF 中位数 0.247。
- burst/quiet 的 2×2 Fisher 双侧精确检验为 **p=1.0**。样本只有 13 个 episode，且唯一事件落在 burst 组；这只能说明当前数据不足以拒绝“无关联”，不能证明 burst 不会增加 timeout 风险。
- memory PSI `some` 的 episode 平均约为 burst 0.35%、quiet 0.48%；IO PSI `some` 约为 burst 0.068%、quiet 0.084%。没有看到与该事件同步的 memory/IO PSI 峰值证据。

窗口内常见 top cgroup 是 `f1cfd.slice/f1st-trim-m1.service`、`f1cfd.slice/f1st-trim-m2.service`、`f1cfd.slice/f1med-r02.service`、`gaius.slice/*`、`system.slice/f1st-r02-*-mp.service`。高 load 的 timeout 窗口还出现 `md-lane-cx-identity-r10-prep.service`、`sunshine-desktop.service` 和多个 docker scope。

## 外部来源判断

有来源与 burst 同时出现，但不能从这份 trace 证明单一来源是原因：

- **f1 CFD/仿真服务**：`f1cfd.slice/f1med-r02.service`、`f1cfd.slice/f1st-trim-m1/m2.service` 在多个 burst 窗口位列 top；它们与高 CPU PSI/load 同步。
- **gaius training**：`gaius.slice/gaius-train-*` 和 `gaius.slice/run-*` 也反复位列 top，timeout 最近样本中同样存在。
- **docker containers**：timeout 最近样本和高 load 窗口中有多个 docker scope，但 trace 没有容器名映射，不能进一步归属。
- **我们自己的模拟/准备任务**：`md-lane-cx-identity-r10-prep.service` 在唯一 timeout 最近样本中排名第一；`f1st-r02-*`、`f1aero-loops-*` 等仿真相关服务也持续出现。

因此，plain 结论是：**唯一可对齐的 Accel timeout 确实落在一个主机 burst 窗口，并且 f1、gaius、docker、自己的准备/仿真服务同时在 top cgroup 中；但事件数只有 1，Fisher 检验没有证据强度，不能声称任一外部来源已被证明是根因。** 其余 timeout 没有 trace 覆盖，无法回答来源是否同步。

## 分析代码与验证

扩展了 [`scripts/analyze_sensor_timeouts.py`](../scripts/analyze_sensor_timeouts.py)，增加 gzip MAVLink、目录递归、墙钟对齐、trace 窗口统计、top cgroup 汇总和 Fisher 精确检验；测试位于 [`tests/test_analyze_sensor_timeouts.py`](../tests/test_analyze_sensor_timeouts.py)。已运行：

```text
5 passed: tests/test_analyze_sensor_timeouts.py -q -p no:xdist
python -m py_compile scripts/analyze_sensor_timeouts.py
```

本报告是活动采集中的一个快照；r9r 和 host trace 仍可能增长，`29-head` 产物也可能稍后出现。重跑下面的命令即可获得新的边界和统计：

```bash
PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp \
  /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python scripts/analyze_sensor_timeouts.py \
  --trace /data/projects/Multi-Detect-env/runs/host-trace/20261107.jsonl \
  artifacts/r21id-r9rb/episodes \
  artifacts/benchmarks/nightly-linux-r21idr3-r24plan-29-head
```
