# Jetson Orin Nano 8 GB 机载 AI 测量

目标硬件是 **Jetson Orin Nano 8 GB**，1024 个 CUDA 核心，CPU/GPU 共享 8 GB RAM；内存带宽约 68 GB/s，MAXN SUPER 模式约 102 GB/s。这里测量 FAST 固定通用词表 YOLOE TensorRT proposer，以及 SLOW v2-r12-multi Q5_K_M GGUF + Q8_0 mmproj / llama.cpp，不启动相机、PX4、ROS、模拟器或 Docker。当前模型以 `configs/onboard/sim-loop-v2r12multi.json` 为准。推荐 JetPack 6.2+、无桌面运行，并记录实际功耗模式与散热条件；其他 Orin 型号或 RTX 工作站不能代替 Nano 真机数据。

主仓库的 [Orin Nano 可行性分析](/D:/Projects/Multi-Detect/docs/orin-nano-feasibility.md)（`docs/orin-nano-feasibility.md`）给出 identity 完整回答约 1.7–2.2 秒、单 token 约 0.6–1.0 秒、planning 约 4–6 秒、FAST 1280 约 50–90 ms 的 Nano **估算**。本文的 benchmark 用板上测量替换这些估算；开发机实测、公开资料推导和 Nano 实测必须分别标记。

## 为什么测这些数

Orin 的统一内存由 Linux、相机缓冲区、YOLOE、llama-server 和 CUDA/TensorRT 共同使用。8 GB 不是模型能单独占用的预算：相机和桌面/系统服务也会占内存，还要留安全余量给运行时波动。报告会记录总 RAM 峰值、可取得的 GPU/共享内存信息、Python 和 llama-server 的 RSS、GPU/CPU 负载、温度和 VDD_IN 功耗。要把相机计入测试，应在运行 bench 时保持实际相机进程运行；如果进程独立，可用 `--monitor-pid camera=PID` 加入相机 RSS。

默认 FAST 路径直接使用 TensorRT Python + CUDA runtime (ctypes)，加载 Orin 构建的裸 engine，不加载 torch、Ultralytics 或 MobileCLIP。脚本分别测 FAST、identity 完整约束生成、identity 单 token、text-only planning，再以固定频率运行 FAST 并发送 identity。每阶段先 warm-up，再重复计时。identity 的 p50/p95 从图像和裁剪编码完成后的 HTTP 请求开始计时，与 `LlamaServerRunner` 的口径一致；不包含 JPEG 编码时间。FAST 包含主机预处理、传输、engine 推理与候选框后处理。只有显式传 `--pytorch-comparison` 才增加开发对照，不能把含 PyTorch 的进程内存当作飞行镜像预算。

Jetson 的 `tegrastats` 报告统一 RAM 和 GR3D 使用率，但不会把 GPU 使用的共享内存从系统 RAM 中单独拆出来。报告会把这个边界写明：Orin 上的 `peak_ram_used_mb` 是系统、相机和 GPU 共享分配的整体峰值；`peak_gpu_shared_memory_mb` 在 `tegrastats` 下会是 null。若设备没有 `tegrastats`，脚本尝试用 `nvidia-smi`；仍没有遥测工具时会写 `not measured`，不会伪造数值。若相机或其他服务由脚本外启动，可用一个或多个 `--monitor-pid camera=PID` 把它们的 RSS 一起采样。

计划请求使用 `trained_key_order_schema(plan_json_schema())`，与运行时默认的 `plan_sorted_keys=True` 一致。这个转换会递归调整键顺序；可选键排在必选键前面时会展开成多个 `anyOf` 分支，不能假设每一级都有 `properties`。完整 identity 使用 `trained_key_order_schema(identity_decision_schema(focus_track_id))`，只允许 `approach_track`、`confirm_target`、`reject_track`，并固定 track。单 token 路径使用固定答案前缀，仅比较三种 decision 首 token 的概率，独立报告，不能与完整生成混为同一阶段。视觉输入保持 `candidate_zoom_v2`、448 长边；完整 identity 默认最多 128 token，planning 最多 768。当前 `OnboardPolicy` 的 `mission=from_text` 会把共用 runner 上限设为 768；若对齐这一模式，增加 `--identity-max-tokens 768`。单 token 可行性分析中的准确率是已有测试集结果，仍需验证实际 Nano 输入和闭环行为。

FAST 在当前代码里是“使用固定通用词表的 class-agnostic proposer”：`YOLOE` 加载微调权重后，先用 `VOCABULARY` 的 20 个词调用 `get_text_pe` / `set_classes`，再按 `conf=0.05`、`imgsz=1280` 推理。这里的 class-agnostic 指它提供候选、由 SLOW 判断任务身份；它不是 YOLOE 原生 prompt-free/LRPC head，也不按任务文字重新设置类别。导出时必须保留同一词表和顺序。虽然权重目录名含 `seg`，当前 checkpoint 实际为 `task=detect`、`YOLOEDetect` head，导出的图不带分割输出。

## 设备准备

在 Nano 上使用 JetPack 6.2+ / aarch64 自带兼容的 TensorRT、CUDA 与 `tegrastats`，准备 Python 的 TensorRT binding、CUDA runtime (ctypes)、NumPy、OpenCV、Pillow 和 llama.cpp `llama-server`。飞行镜像使用 headless 配置，不安装或加载 torch、Ultralytics、MobileCLIP。`--pytorch-comparison` 仅供已有兼容 PyTorch CUDA 环境的开发对照，不属于部署必需项。记录 `nvpmodel -q`，支持时用 MAXN SUPER，并确认散热和供电。缺少依赖时记录所需包及版本，不在 benchmark 中自动安装或下载。

把仓库、地面导出的 ONNX、Nano 本机构建的 TensorRT engine、Qwen3-VL-2B LoRA merged Q5_K_M GGUF（约 1.26 GB）、Q8_0 mmproj（约 0.45 GB）、identity JSONL、对应图像和 PNG 帧放到 Nano 可读的存储位置。只运行 engine 时无需 `.pt` 或 600 MB 的 MobileCLIP 文件。保留 JSONL 图像路径的相对目录关系；脚本也会尝试按 JSONL 所在目录解析相对路径。

```bash
cd "$HOME/Multi-Detect"
mkdir -p artifacts/exports/orin-proposer
mkdir -p artifacts/models/slow-system-lora-v2-r12-multi-merged-gguf
mkdir -p artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503
mkdir -p artifacts/datasets/slow-system-sft-v2-r4-prop
mkdir -p artifacts/datasets/sim-teacher
mkdir -p artifacts/recordings/orin-bench
mkdir -p artifacts/benchmarks

# Replace source paths with files on the mounted local media.
cp /media/bench/slow-system-v2r12multi-merged-Q5_K_M.gguf \
  artifacts/models/slow-system-lora-v2-r12-multi-merged-gguf/
cp /media/bench/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
  artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/
cp /media/bench/proposer-1280.onnx /media/bench/proposer-960.onnx \
  artifacts/exports/orin-proposer/
cp /media/bench/test.jsonl \
  artifacts/datasets/slow-system-sft-v2-r4-prop/test.jsonl
cp -a /media/bench/sim-teacher/. \
  artifacts/datasets/sim-teacher/
cp -a /media/bench/png \
  artifacts/recordings/orin-bench/
```

benchmark 的 YOLOE 权重和 Q8_0 mmproj 路径对应 `scripts/onboard_policy.py`，GGUF 路径对应 `configs/onboard/sim-loop-v2r12multi.json` 的 v2-r12-multi 覆盖；`onboard_policy.py` 本身仍保留旧 v2-r2-seg 默认值，不能把未带配置的默认值当作当前部署模型。若 Orin 上文件位置不同，显式传入对应路径。记录帧目录应含 PNG；identity JSONL 应类似 `artifacts/datasets/slow-system-sft-v2-r4-prop/test.jsonl`，每行至少含 `image_path`、`task_text`、`memory`、`candidates`、`focus_track_id`。这个 JSONL 的 `image_path` 通常引用 `artifacts/datasets/sim-teacher/` 下的原图；这些图也要复制到 Orin 的对应仓库相对路径，或改写 JSONL 路径。脚本默认抽取前 8 条 identity 行和前 8 张 PNG，避免不知情地跑完整大数据集；用 `--identity-limit 0` 或 `--frame-limit 0` 才会使用全部数据。

## 在开发机离线导出 FAST，再在 Orin 构建 engine

`scripts/export_proposer_for_orin.py` 固定使用 CPU；设置离线环境并阻止网络连接和 Ultralytics 下载，在工作树内保存缓存与产物。它从 `onboard_policy.VOCABULARY` 获取同一套 20 词顺序，在 CPU 上完成 `get_text_pe` / `set_classes`，由 Ultralytics 把 embeddings 融入 head。Orin 运行该 ONNX / engine 不需要 text encoder。脚本拒绝原生 LRPC prompt-free checkpoint 和非 detection checkpoint，避免默默导出另一条模型路径。

开发机已具备 PyTorch 2.6.0+cu124、Ultralytics 8.4.92、ONNX 1.22.0、ONNX Runtime 1.29.0 和 CLIP tokenizer；实际运行未用 GPU、未下载或安装任何依赖。所需本地权重为 `best.pt`（26,608,445 bytes）和 `mobileclip_blt.ts`（599,764,649 bytes）。后者只用于导出前编码通用词表，bench 的 PyTorch 路径同样需要它；仅运行 engine 不需要复制它。缺少依赖或资产时脚本直接失败，不执行 pip。若未查询包索引而无法得知缺失下载的大小，错误中会明确标为 unknown。

在当前 Windows 开发工作树执行以下两次导出；源文件只读，产物写在当前工作树的 `artifacts/exports/orin-proposer/`：

```powershell
python scripts/export_proposer_for_orin.py `
  --weights D:/Projects/Multi-Detect/artifacts/experiments/yoloe-v2-r3-seg-drop-full/train/weights/best.pt `
  --weights-dir D:/Projects/Multi-Detect/weights --imgsz 1280
python scripts/export_proposer_for_orin.py `
  --weights D:/Projects/Multi-Detect/artifacts/experiments/yoloe-v2-r3-seg-drop-full/train/weights/best.pt `
  --weights-dir D:/Projects/Multi-Detect/weights --imgsz 960
```

其他开发机显式传入本地 `--weights` / `--weights-dir`；默认指向本仓库下相同路径。默认导出 batch 1、静态方形、FP32、opset 17、`dynamic=False`、`simplify=False`、`nms=False`。FP16 优化交给 Orin 上的 TensorRT。脚本随后运行 `onnx.checker`，并用同一份随机 RGB 输入比较 CPU PyTorch 与 ONNX Runtime：boxes 容差 `rtol=1e-4, atol=0.01`，scores 容差 `rtol=1e-4, atol=1e-5`；这验证原始输出数值，不代表已测 Orin 精度或速度。每个图旁的 `.export.json` 记录源权重 SHA-256、图 SHA-256、词表、版本、尺寸和数值对照结果。

本次 CPU 实际导出结果如下；二者都通过图检查和 CPU 数值对照：

| ONNX 产物 | 文件大小 | opset | `images` 输入 | `output0` 输出 |
| --- | ---: | ---: | --- | --- |
| `proposer-1280.onnx` | 38,422,217 bytes（36.64 MiB） | 17 | float32 `[1,3,1280,1280]` | `[1,24,33600]` |
| `proposer-960.onnx` | 38,128,215 bytes（36.36 MiB） | 17 | float32 `[1,3,960,960]` | `[1,24,18900]` |

输出的 24 通道是 4 个像素坐标 `xywh` 和 20 个类别分数；没有额外 objectness 通道、没有内置 NMS、没有 masks。主机侧必须做与 Ultralytics 等价的 RGB / letterbox / `[0,1]` 归一化、`conf=0.05` 过滤和 class-agnostic NMS，再把框映射回原图。静态 engine 使用完整方形 letterbox；PyTorch 默认矩形最小填充在非方形原图上可能使用较少像素，不能把相同的请求 `imgsz` 当作相同的像素工作量。

把两个 ONNX 和 `.export.json` 复制到 **Orin**。在 Orin 的 JetPack / TensorRT 环境中构建以下两个 engine；不能在 x86 开发机生成 engine 后复制到 Orin：

```bash
mkdir -p artifacts/exports/orin-proposer
trtexec_bin=$(command -v trtexec || true)
if [ -z "$trtexec_bin" ]; then trtexec_bin=/usr/src/tensorrt/bin/trtexec; fi
set -o pipefail
"$trtexec_bin" \
  --onnx=artifacts/exports/orin-proposer/proposer-1280.onnx \
  --saveEngine=artifacts/exports/orin-proposer/proposer-1280-fp16.engine \
  --fp16 --memPoolSize=workspace:512 --skipInference \
  2>&1 | tee artifacts/exports/orin-proposer/build-1280.log
"$trtexec_bin" \
  --onnx=artifacts/exports/orin-proposer/proposer-960.onnx \
  --saveEngine=artifacts/exports/orin-proposer/proposer-960-fp16.engine \
  --fp16 --memPoolSize=workspace:512 --skipInference \
  2>&1 | tee artifacts/exports/orin-proposer/build-960.log
sha256sum artifacts/exports/orin-proposer/*.engine
```

输入尺寸已固定在 ONNX 图内，因此这两条命令不再传 `--shapes` 或动态 profile。`workspace:512` 的单位为 MiB，是 builder tactic workspace 上限，不是进程总 RAM 上限；构建峰值也可能高于推理峰值。构建时停止模型推理，两个尺寸顺序构建，并保留日志、原生命令退出码和 engine hash。`--fp16` 允许 TensorRT 为支持的层选 FP16，不保证所有层都为 FP16；这里仍使用 float32 I/O。若本机已有 TensorRT 版本不接受某个参数，先核对该版本 `trtexec --help`，不要自动下载替代工具。构建后按下文 benchmark 和内存步骤在实际 Orin 验证。

## 执行命令

先在 Orin 上确认解释器和硬件工具，再检查参数：

```bash
python3 --version
python3 -c 'import tensorrt, numpy, cv2; print(tensorrt.__version__, numpy.__version__, cv2.__version__)'
python3 -c 'from cuda import cudart; print(cudart.cudaRuntimeGetVersion())'
command -v llama-server
command -v tegrastats
python3 scripts/bench_orin_onboard.py --help
```

benchmark 默认启动本机 `llama-server`，使用 `-ngl 99`、`-c 2048`、`-np 1`、端口 9941；结束时自动停止。**这是 benchmark 默认值的调整，没有修改机载运行时**：现有 `OnboardPolicy` 调用的 `LlamaServerRunner` 仍默认 4096 context。二者均未指定 `-ctk` / `-ctv`，预算按 F16 K/V，实际以启动日志为准。先测 2048，再用 `--context 4096` 做相同数据的对照；上下文溢出必须报告，不能截断后仍宣称等价。可行性文档中约 1270 prompt + 最多 768 output 已接近 2048，具体任务仍须用真实 token 数验证。

```bash
python3 scripts/bench_orin_onboard.py \
  --frames-dir artifacts/recordings/orin-bench/png \
  --tensorrt-engine artifacts/exports/orin-proposer/proposer-1280-fp16.engine \
  --identity-jsonl artifacts/datasets/slow-system-sft-v2-r4-prop/test.jsonl \
  --gguf-model artifacts/models/slow-system-lora-v2-r12-multi-merged-gguf/slow-system-v2r12multi-merged-Q5_K_M.gguf \
  --mmproj artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
  --llama-server "$(command -v llama-server)" \
  --ngl 99 --context 2048 --repetitions 5 --warmup 1 \
  --output artifacts/benchmarks/orin-onboard-benchmark.json
```

`--llama-server` 可省略，从 `PATH` 搜索。无需手动启动 server，脚本管理自己启动的进程。默认原生 TensorRT 路径直接加载 `trtexec` 生成的裸 engine，不经过 Ultralytics 元数据封装；engine 必须是上述 20 类、相同顺序和输出布局。换测 960 时使用 `--tensorrt-engine artifacts/exports/orin-proposer/proposer-960-fp16.engine --concurrent-imgsz 960`，保存到不同 `--output`；固定尺寸不符的阶段记为 `not measured`。engine 必须在 Nano 的 JetPack/TensorRT 环境构建。`--concurrent-yolo-hz` 调整并行 FAST 频率，先测 2–5 Hz，再按任务需要提高。`--planning-text` 可重复提供任务文字；`--planning-jsonl` 可加入 `kind: mission_plan` 和 `prompt` 字段的计划行。

只有开发对照才增加 `--pytorch-comparison --yoloe-weights /path/to/best.pt`，同时准备本地 MobileCLIP 和匹配 JetPack 的 PyTorch/Ultralytics；为对照单独保存报告，勿混入飞行镜像的内存结论。运行 engine 的 Nano 飞行镜像不需要这三个包或资产。

命令会在 JSON 报告旁写一份同名 Markdown 摘要。若缺少 llama-server、GGUF/mmproj、CUDA、TensorRT engine 或遥测工具，相关阶段会记成 `not measured`，其他可运行阶段仍会执行。请勿把 RTX 工作站上的时间或独立模型的时间当成 Orin 整机数据。

## Orin Nano 8 GB 统一内存预算

下面是启动前的容量规划，**没有任何一项声称已在 Orin 上测得 RAM 占用**。本地只核对了模型文件字节数：v2-r12-multi Q5_K_M 为 1,257,880,736 bytes（1199.61 MiB，1.258 GB），Q8_0 mmproj 为 445,053,216 bytes（424.44 MiB，0.445 GB）。文件大小不等于运行时常驻内存；mmap 页、GPU 分配、缓存和计算缓冲区可能改变实际总量。表中同时给出十进制 GB 和二进制 MiB，避免把两种单位混用。

| 组成 | 初始 RAM 预算（十进制 GB / 约 MiB） | 依据与状态 | Orin 上的核实方式 |
| --- | ---: | --- | --- |
| Headless JetPack OS、驱动与必要后台服务 | 1.0–1.5 GB（约 954–1431 MiB） | 可行性文档的估算；桌面环境会更高 | 无模型、无相机时的 `tegrastats` / `MemAvailable` 基线 |
| Qwen3-VL-2B v2-r12-multi Q5_K_M 权重 | 1.25788 GB（1199.61 MiB） | 文件大小已实测；运行时常驻量仍是估算 | llama-server 日志中的 model buffer；进程 PSS 和系统 RAM 差值 |
| Q8_0 mmproj 权重 | 0.44505 GB（424.44 MiB） | 文件大小已实测；运行时常驻量仍是估算 | 日志中的视觉模型/投影 buffer；图像请求后重新取样 |
| KV cache：2048 context、单 slot、F16 K/V | 0.23488 GB（224 MiB） | 架构计算估算，未在板上测量 | 日志中的 KV buffer、cache type、`n_ctx` / `n_ctx_per_seq` |
| llama.cpp 文本/视觉计算缓冲与 CUDA context | 0.3–0.5 GB（约 286–477 MiB） | 可行性文档的工程估算；图片数量、batch 和 llama.cpp 版本相关 | 分别记录已加载空闲、identity、planning 峰值及日志 compute buffer |
| YOLOE FP16 TensorRT engine、执行上下文与激活 | 0.5–0.8 GB（约 477–763 MiB） | 可行性文档的工程估算，1280/960 均待实测 | 单 engine 的 `trtexec` 日志和统一 RAM 增量；engine 文件大小单独记录 |
| 无 PX4/ROS 的 Python 主机进程、跟踪、图像与候选裁剪 | 0.5 GB（约 477 MiB） | 可行性文档的工程估算，不含已列出的模型/engine 分配 | `/proc/PID/smaps_rollup` 的 PSS、RSS；与进程启动前基线比较 |
| 合计，不含独立相机额外缓冲 | 约 4.7–5.7 GB（约 4482–5436 MiB） | 可行性文档的四舍五入估算，不是容量保证；按表中精确项相加约 4.74–5.74 GB | 必须用 FAST + SLOW 同时驻留、同时工作时的统一 RAM 峰值判定 |

KV 估算使用已缓存 Qwen3-VL-2B 的 `config.json`：28 层、8 个 KV heads、head dimension 128。F16 K/V 的基础占用为 `2048 × 28 × 8 × 128 × 2（K+V）× 2 bytes = 234,881,024 bytes = 224 MiB`，不包含对齐和其他推理缓冲。作为对照，4096 context 为 448 MiB。Q5_K_M 权重量化不会自动把 KV cache 量化。视觉 token 也消耗同一个 context；不能另把视觉 token 数乘一次、再重复计入已分配 KV cache。若启动日志显示上下文长度、KV 数据类型或布局不同，应替换此估算。表中的 GB 是十进制，MiB 是 2^20 字节；这是启动前估算，不能替代 Orin 实测。

内存测量按以下步骤执行，保留原始日志以便重查：

1. 记录硬件型号、JetPack / TensorRT / llama.cpp 版本、`nvpmodel -q` 和散热状态；只有运行 `--pytorch-comparison` 时才记录 PyTorch / Ultralytics 版本。停止模拟器、Docker、PX4、ROS 及无关 GPU 任务，保持随后实际部署要保留的系统服务。若要纳入真实相机，另做一次相机开启的同条件测量，记录分辨率、帧率和缓冲数量。
2. 在没有模型进程时采样至少 30 秒。以下命令保留 100 ms 的全程原始遥测、系统内存与 swap 基线；结束整个测量后停止本次启动的 tegrastats。不要对统一内存再加一份所谓独立 GPU 显存。

   ```bash
   mkdir -p artifacts/benchmarks/orin-memory
   tegrastats --interval 100 > artifacts/benchmarks/orin-memory/tegrastats.log 2>&1 &
   telemetry_pid=$!
   cat /proc/meminfo > artifacts/benchmarks/orin-memory/meminfo-baseline.txt
   free -b > artifacts/benchmarks/orin-memory/free-baseline.txt
   sleep 30
   ```

3. 单独启动与 benchmark 相同模型参数的 server，保留启动日志。等 `/health` 返回 `ok` 后空闲至少 30 秒，记录 RAM 和进程 PSS；日志同时给出实际 model / KV / compute buffer。此阶段不启动第二个 llama-server。

   ```bash
   llama-server \
     -m artifacts/models/slow-system-lora-v2-r12-multi-merged-gguf/slow-system-v2r12multi-merged-Q5_K_M.gguf \
     -mm artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
     -ngl 99 -c 2048 -np 1 -n 768 --host 127.0.0.1 --port 9941 \
     --no-webui --cache-prompt --cache-reuse 64 \
     > artifacts/benchmarks/orin-memory/llama-startup.log 2>&1 &
   llama_pid=$!
   curl --fail http://127.0.0.1:9941/health
   sleep 30
   cat /proc/$llama_pid/smaps_rollup > artifacts/benchmarks/orin-memory/llama-idle-smaps.txt
   cat /proc/meminfo > artifacts/benchmarks/orin-memory/meminfo-llama-idle.txt
   kill "$llama_pid"
   wait "$llama_pid"
   ```

   若第一次 health 查询仍在加载，重复查询直至 `ok`，再开始这 30 秒空闲采样。用统一 RAM 相对基线的差值估算整段加载成本；PSS 用于解释进程共享页，不能把进程 RSS、模型文件大小和 tegrastats RAM 再相加。
4. 按上面的完整 benchmark 命令运行真实 full identity、one-token identity、planning 及并行阶段，增加 `--tegrastats-interval-ms 100 --repetitions 20`。检查报告实际 `context=2048`、模型路径、请求样本数、token timing 字段和重叠次数；随后用 `--context 4096` 对同一数据做对照。默认 native TensorRT engine 会在 identity / planning / 并行阶段驻留；只有显式传 `--pytorch-comparison` 才加载 PyTorch proposer。
5. 对 TensorRT 部署另测同一 Orin 上的 engine：先用 `trtexec --loadEngine=... --duration=60` 记录单 engine 稳态与峰值，再让该 engine 与同参数 llama-server / 实际主机进程同时驻留并发送实际工作负载，连续运行至少 10 分钟。记录 1280 和 960 各自的 engine、build 参数、latency、统一 RAM 峰值、MemAvailable 最低值、swap 活动、温度和功耗；不能用各独立阶段峰值的相加代替并行实测。
6. 在实际主机进程运行时，通过 `--monitor-pid host=PID`（相机独立时再加 `--monitor-pid camera=PID`）采样 RSS；需要区分共享页时同步保存 `/proc/PID/smaps_rollup`。完成后记录最终内存并停止本次采样：

   ```bash
   cat /proc/meminfo > artifacts/benchmarks/orin-memory/meminfo-final.txt
   kill "$telemetry_pid"
   wait "$telemetry_pid"
   ```

报告验收依据是整机真实工作负载的 `peak_ram_used_mb` 和剩余容量，而不是表格合计；swap 或 page cache 可能让单一 RAM 指标误导，应同时检查 `/proc/meminfo` 的 `MemAvailable`、`SwapTotal` / `SwapFree`、原始 tegrastats，以及长时间稳定性。相机不在上述合计中，实际使用时必须保留其占用并重测。

## 看报告与判定

先检查每个 phase 的 `status`、`errors`、`latency.count`，再看 p50/p95 和 `resources`。JSON 保留每笔延迟，Markdown 供快速比较。identity 与 planning 的主要比较值是 `latency.p95_s`。YOLOE 各尺寸应同时看 p95、GPU 负载和 RAM；并行阶段要比较 identity 的 p95 是否明显恶化，并确认排程漏执行数 `missed_schedule_slots` 和实际重叠请求数 `overlapping_identity_calls`。

报告中的 `identity` 是完整约束 identity，`identity_one_token` 是固定答案前缀下只取首 token 的快速阶段；两者必须分开看，不能合并成一个准确率或延迟。每笔 SLOW 样本应同时检查 `prompt_n`、`prompt_ms`、`predicted_n`、`predicted_ms`（字段缺失时保留为 `not measured`），并与整笔 HTTP / runner latency 区分。默认每个 FAST 尺寸有 8 张图 × 5 次重复，即 40 个延迟样本，identity 同样最多 40 个，planning 默认 15 个。p50 用中位数，p95 用 nearest-rank；样本少时 p95 接近最大值，应增加 `--repetitions` 再判断。native TensorRT 是默认部署路径；`--pytorch-comparison` 只用于开发机或已准备好兼容环境的对照，不能混入飞行镜像结论。

RAM、GPU 显存字段虽以 `_mb` 命名，按工具的 MiB 口径读取；RSS 在 JSON 中以 bytes 保存。Markdown 的 RSS 列是所有受监测进程中最大的单进程峰值，各进程详情见 `peak_rss_bytes_by_process`。不要把 RSS 相加当成统一内存总量，因为共享页和峰值时刻可能不同。遥测默认每 500 ms 采样一次，采样峰值可能漏过更短的瞬时尖峰；需要时用 `--tegrastats-interval-ms 100` 重跑。

先把可行性文档的数字当作待验证目标：完整 identity 约 1.7–2.2 秒，one-token 约 0.6–1.0 秒，text-only planning 约 4–6 秒，FAST 1280 TensorRT 约 50–90 ms；这些是 Nano 的事前估算，不是硬性通过线。工程上仍建议把任一阶段总 RAM 峰值控制在 **6.5 GB（十进制，约 6199 MiB）以下**，即 `peak_ram_used_mb < 6199`，并把实际值、`ram_total_mb - peak_ram_used_mb`、`MemAvailable` 和 swap 一起记录。不要假定标称 8 GB 全部可用；`tegrastats` 报告统一内存，包含系统、相机和 GPU 共享分配；`peak_gpu_shared_memory_mb: null` 表示不能单独拆分 GPU 占用。并行时 YOLOE 的漏排数应为 0 或很少，identity p95 不应大幅超出单独测试值。

任一门槛不过，先按以下顺序调整并重跑同一组数据：

1. planning 慢时先用 Q4_K_M GGUF 做对照，mmproj 保留 Q8_0。当前脚本用同一个 GGUF 测 identity 和 planning，仓库里的 `OnboardPolicy` 也复用同一个 llama-server；所以现有实现还不能只给 planner 换量化。把 `--gguf-model` 指向 Q4 文件重跑并对比两类请求；若要只让 planner 使用 Q4，需先给运行时增加独立的 planner 模型路由，再验证 identity 仍用 Q5_K_M 且视觉判断没有退化。
2. RAM 或 YOLOE 时延过高时，先降低 YOLOE `imgsz`（1280 降到 960，再到 640）。
3. YOLOE 仍无法跟上目标频率时，改用 Orin 上为相同模型/输入尺寸构建的 TensorRT engine。
4. 并行或 RAM 仍超标时，减少每个步骤的慢模型 identity 呼叫次数，并重新跑并行阶段确认。

6.5 GB 是保守的初始工程门槛，不是 JetPack 的硬件限制。判读时使用同一 Orin、电源模式、散热条件和后台服务状态；长时间运行仍须额外观察温度与降频。
