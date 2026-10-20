# Jetson Orin Nano 8GB 算力可行性（2026-10-15）

目标机载电脑是 **Jetson Orin Nano 8GB**：1024 个 CUDA 核心，CPU 和 GPU 共用 8 GB 内存，内存带宽约 68 GB/s（Super 模式约 102 GB/s）。开发机 RTX 4060 的显存带宽是 272 GB/s，所以开发机上的耗时**不能直接当成机上耗时**。本页把每一项分成“开发机实测”和“Orin Nano 估算”，估算都要在真机上用 `scripts/bench_orin_onboard.py` 复核。

## 结论

- **能跑，但要按 Orin Nano 来设计。** 一个 2B 视觉语言模型（慢系统）+ 一个小检测器（快系统）加起来约 5–5.5 GB，8 GB 内能放下，前提是机上不跑 PyTorch、不加载 600 MB 的 MobileCLIP 文本编码器（词表的文本向量在地面预先算好）。
- **慢系统每次判断约 2 秒（估算），优化后约 1 秒。** 其中一大半花在把答案一个字一个字吐出来上。新增的“单 token 判断”只生成 1 个 token，开发机上身份准确率不降（96.8% 对 96.4%），耗时减半以上。
- **飞机是“走走停停”地思考。** 判断期间飞机悬停，不会边飞边等；所以判断慢只影响任务总时长，不影响安全。减少要判断的次数（同一物体不反复问）和单次耗时同样重要。

## 一次身份判断的成本

开发机（RTX 4060，与仿真共用 GPU）用当前 v2-r18-multi Q5_K_M 实测 12 次（旧 r12 结果只作历史基线）：

| 项目 | 开发机实测 | Orin Nano 估算 | 依据 |
| --- | --- | --- | --- |
| 输入图片 | 1 张整帧（最长边 448）+ 1–3 张候选放大图 | 同左 | |
| 提示长度（含图片） | 约 470 token | 同左 | |
| 视觉编码 | 计入下行 | 约 0.2–0.5 s | 公开测试：2B 级 VLM 在 Orin Nano 上单图首 token 约 0.1 s |
| 预填充 | 约 180 ms | 约 0.35–0.45 s | 公开测试：1.7B Q4_K_M 预填充约 1,400 token/s |
| 解码（完整答案约 40 token） | 约 295 ms | 约 1.1–1.3 s | 公开测试：1.7B Q4_K_M 解码约 39 token/s |
| **合计（完整答案）** | **p50 0.52 s** | **约 1.7–2.2 s** | |
| **合计（单 token 判断）** | **p50 0.22 s** | **约 0.6–1.0 s** | 解码只剩 1 个 token |

单 token 判断的原理：机上每次只问“焦点候选是不是任务要的东西”，答案里的 `args` 已被约束固定，`reason` 对决策无用，真正决定结果的只有 `"decision":"` 后面的第一个 token（approach / confirm / reject 三个首 token 不同）。所以把固定前缀直接喂给模型，只算 1 个 token 的概率，取三者中最大的。243 条测试集上：身份准确率 96.8%（完整生成 96.4%），干扰物拒绝 95.8%（95.0%）。配置项 `fast_identity`，默认关闭，闭环飞行验证后再打开。

## 起飞时编任务计划

| 项目 | 开发机实测 | Orin Nano 估算 |
| --- | --- | --- |
| 提示长度 | 约 1,270 token | 同左 |
| 输出长度 | 约 90–150 token | 同左 |
| 耗时 | p50 0.74 s（空闲 4060） | 约 4–6 s，每个任务只做一次 |

## 快系统（候选框）

YOLOE-11s-seg，输入 1280。公开测试：YOLO11n / YOLOv8n 在 Orin Nano 上 TensorRT FP16、640 输入约 7–21 ms。11s 比 n 大约 3 倍计算量，1280 输入是 640 的 4 倍像素，估算每帧约 50–90 ms（10–20 Hz），只用检测头、不出分割掩膜还能再省。与慢系统共用 GPU，慢系统判断期间快系统降到 2–5 Hz 即可（飞机在悬停）。备选：输入降到 960（小目标召回要重测）。

## 内存预算（8 GB 共用）

| 占用 | 估算 |
| --- | --- |
| JetPack 系统（无桌面） | 1.0–1.5 GB |
| 慢系统权重 Q5_K_M | 1.26 GB |
| 视觉投影 mmproj Q8_0 | 0.45 GB |
| KV 缓存（上下文 2048；每 token 约 112 KB） | 0.23 GB（4096 时 0.47 GB） |
| llama.cpp 计算缓冲 | 0.3–0.5 GB |
| YOLOE TensorRT 引擎 + 激活 + CUDA 上下文 | 0.5–0.8 GB |
| 主机进程（图像、跟踪、执行器） | 约 0.5 GB |
| **合计** | **约 4.7–5.7 GB** |

上下文 2048 够用：身份判断约 470 token，编计划约 1,270 + 输出 ≤ 768。机上不能加载 PyTorch + ultralytics + MobileCLIP（光 PyTorch 就要 1 GB 以上），快系统要导出成 ONNX → TensorRT 引擎，词表文本向量离线算好。

## 每次任务要判断多少次

仿真记录显示同一个物体会被跟踪成多个轨迹 ID、被反复判断（一个电箱一次飞行 18 次）。已有两项改进：按地点记住一致的判断（`memory_enabled`，181 次飞行回放可少问 20%），以及正在修的轨迹碎片问题。按每次约 1 s、一次巡逻约 50–100 次判断估算，思考时间约 1–2 分钟。

## 还需要在真机上做的

1. 刷 JetPack 6.2+，开 MAXN SUPER 功耗模式，关桌面。
2. 按 `docs/orin-onboard-benchmark.md` 跑基准：慢系统完整 / 单 token 两种模式、编计划、YOLOE 引擎 960 / 1280，记录耗时、峰值内存、温度。
3. 把上表的“估算”换成实测，再决定快系统输入尺寸和判断频率。

公开参考：[Turing Pi：Orin Nano 8GB llama.cpp 测试](https://turingpi.com/jetson-orin-nano-8gb-llm-benchmark/)、[Hokwang Choi：Orin Nano 上的 VLM 三种运行时](https://hokwangchoi.com/blog/vlm-benchmarks/)、[Ultralytics：YOLO11 在 Orin Nano Super 上](https://www.ultralytics.com/blog/ultralytics-yolo11-on-nvidia-jetson-orin-nano-super-fast-and-efficient)。

## Planner/identity split (2026-10-16)

配置 `planning_model` 后，策略先加载纯文本 planner，单次任务规划预计耗时约 4–6 秒；随后关闭
server 并等待端口释放，再加载 identity 模型。额外启动/卸载时间应按约 4–6 秒加两次模型加载时间
预算，最终数值必须在目标 JetPack 镜像上实测，不能拿开发机时延冒充 Nano 结论。任意时刻只有一个
模型进程常驻，因此稳态内存仍按上文约 4.7–6.7 GB 预算；planner 因为请求不含图片可以省略
`mmproj`（配置 mapping 显式提供时才启用），identity 继续使用现有视觉 projector。planner 启动或校验失败会计入初始化规划失败并
回退到 identity；只有通过 schema 和 mission-package 检查的计划才允许起飞。split eval 配置使用
v2-r14-multi 规划（显式对比项）、v2-r18-multi 身份判断。摘要记录 `swap_duration_s`（planner 关闭开始到
identity ready）和 `swap_completed_monotonic_s`；模型 hash 使用按文件 size/mtime_ns 缓存，hash
失败记录为 `unknown`，不阻断起飞。
