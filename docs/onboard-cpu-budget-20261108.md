# Onboard CPU-only 性能基线（2026-11-08）

## 结论与状态

本轮在双路 AMD EPYC 7402 主机上建立了可重复的 CPU-only 组件基线，没有 Jetson 实机，因此 Jetson 目标门槛**尚未通过**。这份主机数据只用于比较本工作负载的阶段成本和安排 Jetson 复测，不能换算成 Jetson 延迟，也不能代表完整 detector/model inference、任务吞吐或 onboard 闭环实时性。

基准热点按当前合成规模排序为：64×64 IoU association、96×72 RGB 转灰度/候选计数。它们是明确的 profile 机会，不是 production flight-path 优化结论。没有做运行时代码优化，故没有优化前后输出等价性或 p95 改善声明。轻量 primitive/policy 代理和 JSON 序列化不是这组 CPU microbenchmark 的主热点。

## 计时与设备

- 主机：AMD EPYC 7402 24-Core Processor，2 socket、48 core / 96 logical CPU，x86_64，Linux 7.0.0-14-generic；该主机有高并发能力，不是 Jetson-class CPU。
- Python：`/data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python`，Python 3.12.16；无 torch/模型加载。单进程、单 Python thread；`taskset -c 0` 固定到逻辑 CPU 0，`OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1`。
- 基准 revision：`494098f45ef151edbb1b306961cec48b11a8290f`。最终交付脚本 SHA-256：`bd09c19be8a8ab85eb7df3fe73c24e4e41c7bd09a328a8c8f0561d3748bd80ad`。
- 定时使用 `time.perf_counter_ns()`，统计每次函数调用的 wall-clock elapsed ms。每个阶段先热身 3 次，再做 3 组、每组 30 次，共 90 个样本；下表每格给出三次**独立进程运行**的 p50/p95 范围。p95 采用 nearest-rank 经验分位数。输出 RSS 是 Linux `ru_maxrss` 进程高水位，不是单阶段增量。
- 本地 replay metadata 固定输入：`tests/fixtures/onboard_replay/episode.json` + `steps.jsonl`；1 个 step row。两文件按文件名和内容顺序合并散列 SHA-256：`1cfc7ac6eee87a4232f8a54f719e0e38f6c9bb505c882a00643e1cc6d8627af4`。该 fixture 无 RGB 图像，所以 RGB workload 使用确定性合成 96×72×3 uint8 字节（20,736 B，SHA-256 `ef98f608f14925b27ff53b008b9fa4a19b62b1f80db6f53422efa3ab0f81c03d`）。association 使用 64 candidates × 64 tracks；日志代理序列化 319 B record。
- 三次最终进程均报告 affinity `[0]`，RSS 峰值均为 48,976 KiB（约 47.8 MiB）。

## CPU 阶段基线

数值单位均为 ms/调用。区间是三次独立运行各自统计量的最小值至最大值，不是样本置信区间。

| 阶段 / 实现口径 | 输入规模 | p50 范围 | p95 范围 |
| --- | --- | ---: | ---: |
| replay decode | 1 JSONL step + 1 episode JSON | 0.088–0.126 | 0.114–0.150 |
| detector/preprocess 代理 | 合成 96×72 RGB；Python RGB→灰度和阈值候选计数，无模型 | 3.035–3.468 | 3.591–5.068 |
| planner/policy 代理 | 固定小型观测摘要，构造 `ActionPrimitive` 并转换 NED | 0.0067–0.0074 | 0.0082–0.0111 |
| geometry/association | 64×64 候选/track pair，复用 onboard `_iou` 并计数邻近匹配 | 5.744–6.806 | 7.268–10.866 |
| serialization/logging 代理 | 319 B JSON record，排序紧凑 JSON + SHA-256 | 0.016–0.030 | 0.018–0.049 |

association p95 首轮为 10.87 ms，serialization 首轮为 0.049 ms；它们较随后两轮偏高。固定 CPU affinity 后仍有调度尾部波动，应把三次范围理解为此主机短跑观察值。association 的配对量是 O(N×M)，64×64 的成本支持后续做同语义、更真实分布的候选数扫描，但当前结果不足以证明车辆实际候选规模下它会成为端到端主瓶颈。

## Wall time、等待与历史 evidence

这些 profile 阶段测的是本地 CPU 函数调用 elapsed time，不含相机取帧、PX4 command/ACK/primitive completion、模拟器推进或任务等待。固定 replay fixture 只有一个 JSONL step，也没有帧文件，故本次没有测量 frame wait 或任务 wall time。

`docs/operator-abort-wall-20261107.md` 的 66-step episode 将 `command_and_primitive_wait_ms` 记录合计为 4,539.544 s（含 `timeout_ms` 4,017.842 s），前后 frame wait 合计 228.354 s，policy 合计 5.870 s（0.12%）。这些来源数据有包含关系，不能当作互斥项重新累加。它说明该历史 episode 的 wall time 主要是 primitive/executor wait，不能以模型推理时间解释，也不能用本报告微基准预测整次 episode 的耗时。

`docs/sensor-timeout-load-20261107.md`、`docs/timeout-host-trace-20261107.md` 与 `docs/sensor-timeout-followup-20261107.md` 均指出旧 episode 的 host pressure / frame wait 字段存在覆盖缺口；新采集中的时间对齐和 load 共现仍只是相关性材料。这里的 CPU microbenchmark 不测传感器 timeout，不将 profile 延迟与 timeout 关联成根因。`docs/nightly-review-29-head-20261107.md` 的 SITL task wall time/RTF 仅作任务时长参考，不代表 CPU inference latency。

## 可验证机会

1. 对 association 做 `N,M ∈ {16,32,64,128}` 的确定性候选规模曲线，并 profile 实际 onboard 候选/track 分布。只有再在相同帧/replay 上保持匹配结果完全一致、p95 改善高于重复测量噪声、RSS 不回退，才考虑局部优化。
2. 使用固定原始 RGB replay 帧（现有固定 fixture 没有图像），把与部署相同的 decode/resize/颜色转换和候选生成分段计时；在没有本地 CPU detector 权重时仍应将模型 inference 标为未测。不要用本次合成灰度代理代替 detector inference。
3. 分别采集本机相机 frame wait、planner queue/job、PX4 primitive wait 与 serialization/logging 的同一 episode 时间线。现有历史证据已显示 wait 可能主导墙钟，采样要保留互不重叠或明确包含关系的计时口径。

## 复现

```bash
export PYTHONPATH=src:. PYTHONUTF8=1 TMPDIR=$PWD/.tmp
mkdir -p .tmp/onboard-cpu-profile
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  taskset -c 0 /data/projects/Multi-Detect-env/Multi-Detect/.venv/bin/python \
  scripts/profile_onboard_cpu_budget.py \
  --repeats 3 --iterations 30 --warmup 3 \
  --output .tmp/onboard-cpu-profile/profile.json
```

## Jetson 复测门槛

目前没有 Jetson 实测。以下步骤应在指定 Jetson 设备本机执行，并把准确 SoC/SKU、JetPack/L4T、内核、Python 和仓库 revision 一起记录；不要把本主机的绝对延迟作为通过依据。

1. 获取固定输入、脚本和代码 revision；确认 replay/hash 与本报告一致，禁用网络下载、模型服务和 GPU，记录设备空闲/温度/功耗模式、可用 CPU affinity 与内存状态。对于部署 detector 权重，另单独记录本地文件 SHA-256、CPU runtime/provider 和线程数。
2. 使用 Jetson 上适用的 Python 环境运行同一脚本。先通过 `taskset -pc $$` 确认可用 CPU 集合，再将 `taskset -c 0` 中的 `0` 替换为允许的 CPU；运行三次进程，每次相同 `--repeats 3 --iterations 30 --warmup 3`。默认脚本不加载 detector 模型；如果测试模型，必须新增单独阶段/输入 provenance 并确认 provider 是 CPU。
3. 保存三份 JSON，比较每阶段 p50/p95、RSS、线程数、温度/频率和结果签名。之后使用固定 RGB replay 帧与真实 workload 候选规模增加验证；association 优化须逐项匹配输出等价。未在目标 Jetson 上覆盖端到端 sensor-frame→decision 与独立等待计时前，Jetson 性能门槛仍保持未通过。

## 验证记录

- 新增定向测试及 runtime replay/policy timing 测试：17 passed。
- `scripts/run_onboard_gate.py -q -p no:warnings`：`PASS (153s)`。
- `ruff check scripts/profile_onboard_cpu_budget.py tests/test_profile_onboard_cpu_budget.py` 未运行成功：当前环境没有 `ruff` 可执行文件。
- 未运行 SITL、Docker、GPU、模型服务、训练、release-parity、HIL、真实飞行或网络下载；未修改 `artifacts/`、任务 expected、安全阈值、timeout/retry 或历史 evidence 文档。

## 建议的下一个任务

1. 目标：用固定原始 RGB replay 帧和部署一致的 CPU-only decode/resize/颜色转换，在 `N,M ∈ {16,32,64,128}` 候选规模下复测 detector/preprocess 与 geometry/association 的 p50/p95/RSS，并验证 association 输出逐项等价；证据路径：`scripts/profile_onboard_cpu_budget.py`、`src/multidetect/autonomy/onboard_loop.py`、`tests/fixtures/onboard_replay/`、`docs/onboard-cpu-budget-20261108.md`；不得启动 SITL、Docker、GPU、模型服务或下载权重，不得修改任务 expected、安全阈值、timeout/retry、`artifacts/` 或历史 evidence-bound 文档；写入新增 CPU-only benchmark/helper 窄测试和 `docs/onboard-cpu-budget-scaling-20261108.md`。
2. 目标：在一台 Jetson 目标设备上按固定 replay/hash、CPU affinity、线程、热身和三次独立进程运行本报告脚本，另记录真实本地 CPU detector 权重（若存在）的 provider、权重 hash、p50/p95、RSS、温度/频率，并把 sensor/frame wait、PX4 primitive wait、policy/geometry/logging 明确分开；证据路径：`docs/onboard-cpu-budget-20261108.md`、`docs/operator-abort-wall-20261107.md`、`docs/sensor-timeout-load-20261107.md`、`docs/timeout-host-trace-20261107.md`、`docs/sensor-timeout-followup-20261107.md`；不得用桌面主机数据冒充 Jetson，不得启动 SITL/Docker/GPU 训练或网络下载，不得修改 runtime 安全语义、任务门槛、timeout/retry 或共享 `artifacts/`；结果写入 `docs/onboard-cpu-budget-jetson-20261108.md`。
