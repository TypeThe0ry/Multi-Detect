# Orin 机载 AI 测量

这个工具在 Jetson Orin 8 GB 上测量现有机载 AI 组合，不会启动相机或飞控：YOLOE 使用通用类别词表给候选框，慢模型用 llama.cpp 对候选框做身份判断，并按任务文字生成计划。它也能在装有 CUDA 的 Linux 工作站上运行；工作站结果不能代替 Orin 结果。

## 为什么测这些数

Orin 的统一内存由 Linux、相机缓冲区、YOLOE、llama-server 和 CUDA/TensorRT 共同使用。8 GB 不是模型能单独占用的预算：相机和桌面/系统服务也会占内存，还要留安全余量给运行时波动。报告会记录总 RAM 峰值、可取得的 GPU/共享内存信息、Python 和 llama-server 的 RSS、GPU/CPU 负载、温度和 VDD_IN 功耗。要把相机计入测试，应在运行 bench 时保持实际相机进程运行；如果进程独立，可用 `--monitor-pid camera=PID` 加入相机 RSS。

脚本分别测试 YOLOE PyTorch fp16 的 640、960、1280 输入尺寸；可选地测试预先导出的 TensorRT engine；测试带图像和候选裁剪、JSON Schema 约束输出的 llama-server identity 请求；测试 text-only mission planning 请求；最后以固定频率运行 YOLOE，同时发送 identity 请求。每个阶段先做 warm-up，再重复计时。identity 的 p50/p95 从图像和裁剪已经编码完成后的 HTTP 请求开始计时，和现有 `LlamaServerRunner` 的口径一致；报告会把这点写进 `configuration.latency_note`。planning 是 text-only 请求。YOLOE 计时包含 Ultralytics 的预处理、推理和检测结果物化。

Jetson 的 `tegrastats` 报告统一 RAM 和 GR3D 使用率，但不会把 GPU 使用的共享内存从系统 RAM 中单独拆出来。报告会把这个边界写明：Orin 上的 `peak_ram_used_mb` 是系统、相机和 GPU 共享分配的整体峰值；`peak_gpu_shared_memory_mb` 在 `tegrastats` 下会是 null。若设备没有 `tegrastats`，脚本尝试用 `nvidia-smi`；仍没有遥测工具时会写 `not measured`，不会伪造数值。若相机或其他服务由脚本外启动，可用一个或多个 `--monitor-pid camera=PID` 把它们的 RSS 一起采样。

## 设备准备

在 Orin 上准备 JetPack 6 / aarch64 的 PyTorch CUDA、Ultralytics YOLOE、OpenCV、Pillow，以及 llama.cpp 的 `llama-server`。可先按本仓库已有的 `scripts/bootstrap_jetson_orin.sh` 准备通用 Jetson bench 环境；YOLOE/PyTorch 版本要与 JetPack 6 的 CUDA 运行时匹配，不要让普通 pip 安装替换 NVIDIA 提供的 PyTorch。`tegrastats` 通常随 JetPack 提供。运行前停止其他 GPU 密集任务，并确认散热和供电正常。

把仓库、YOLOE 权重、Qwen3-VL-2B LoRA merged Q5_K_M GGUF（约 1.26 GB）、Q8_0 mmproj（约 0.45 GB）、identity JSONL、对应图像和 PNG 录像帧放到 Orin 可读的存储位置。可以用本地 SSD/USB 或已有的授权文件传输方式；下面示例用路径变量，不包含主机名、登录信息或密码。保留 JSONL 中图像路径的相对目录关系；脚本也会尝试按 JSONL 所在目录解析相对路径。

```bash
cd "$HOME/Multi-Detect"
mkdir -p artifacts/experiments/yoloe-v2-r3-seg-drop-full/train/weights
mkdir -p artifacts/models/slow-system-lora-v2-r2-seg-merged-gguf
mkdir -p artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503
mkdir -p artifacts/datasets/slow-system-sft-v2-r4-prop
mkdir -p artifacts/datasets/sim-teacher
mkdir -p artifacts/recordings/orin-bench
mkdir -p artifacts/benchmarks

# 把源路径换成已经挂载的本地介质上的文件位置。
cp /media/bench/slow-system-v2r2seg-merged-Q5_K_M.gguf \
  artifacts/models/slow-system-lora-v2-r2-seg-merged-gguf/
cp /media/bench/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
  artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/
cp /media/bench/best.pt \
  artifacts/experiments/yoloe-v2-r3-seg-drop-full/train/weights/
cp /media/bench/test.jsonl \
  artifacts/datasets/slow-system-sft-v2-r4-prop/test.jsonl
cp -a /media/bench/sim-teacher/. \
  artifacts/datasets/sim-teacher/
cp -a /media/bench/png \
  artifacts/recordings/orin-bench/
```

默认路径对应 `scripts/onboard_policy.py` 使用的 YOLOE 权重、Q5_K_M GGUF 和 Q8_0 mmproj。若 Orin 上文件位置不同，显式传入对应路径。记录帧目录应含 PNG；identity JSONL 应类似 `artifacts/datasets/slow-system-sft-v2-r4-prop/test.jsonl`，每行至少含 `image_path`、`task_text`、`memory`、`candidates`、`focus_track_id`。这个 JSONL 的 `image_path` 通常引用 `artifacts/datasets/sim-teacher/` 下的原图；这些图也要复制到 Orin 的对应仓库相对路径，或改写 JSONL 路径。脚本默认抽取前 8 条 identity 行和前 8 张 PNG，避免不知情地跑完整大数据集；用 `--identity-limit 0` 或 `--frame-limit 0` 才会使用全部数据。

## 执行命令

先在 Orin 上确认解释器和硬件工具，再检查参数：

```bash
python3 --version
python3 -c 'import torch; print(torch.__version__, torch.cuda.is_available(), torch.version.cuda)'
command -v llama-server
command -v tegrastats
python3 scripts/bench_orin_onboard.py --help
```

默认参数会启动本机 `llama-server`，用 `-ngl 99`、4096 context、端口 9941；完成后自动停止该 server。先用一小批录像和 5 次计时重复运行：

```bash
python3 scripts/bench_orin_onboard.py \
  --frames-dir artifacts/recordings/orin-bench/png \
  --yoloe-weights artifacts/experiments/yoloe-v2-r3-seg-drop-full/train/weights/best.pt \
  --identity-jsonl artifacts/datasets/slow-system-sft-v2-r4-prop/test.jsonl \
  --gguf-model artifacts/models/slow-system-lora-v2-r2-seg-merged-gguf/slow-system-v2r2seg-merged-Q5_K_M.gguf \
  --mmproj artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
  --llama-server "$(command -v llama-server)" \
  --ngl 99 --context 4096 --repetitions 5 --warmup 1 \
  --output artifacts/benchmarks/orin-onboard-benchmark.json
```

`--llama-server` 可省略，这时会从 `PATH` 搜索。通常不需要手动启动 server：测量脚本会用指定 GGUF/mmproj、`-ngl` 和 context 在本机启动，结束时停止自己启动的程序。若有对应 YOLOE 通用词表的 TensorRT engine，另加 `--tensorrt-engine /path/to/yoloe.engine`。Engine 的输入尺寸若固定，报告会核对有效输入尺寸，只把实际尺寸相符的结果标为 measured。TensorRT engine 应已 bake 入与机载 YOLOE 相同的 generic vocabulary，并由 Orin 上相同 JetPack/TensorRT runtime 构建；不要把 x86 engine 复制过来。可用 `--planning-text 'Patrol the north side and return home.'` 重复提供任务文字；不提供时会测量内建的三条一般任务文字。`--planning-jsonl` 可加入含 `kind: mission_plan` 和 `prompt` 字段的已记录计划行。`--concurrent-yolo-hz` 可调整并行阶段的 YOLOE 发送频率，`--concurrent-imgsz` 可在 640/960/1280 中选择并行阶段的输入尺寸。

命令会在 JSON 报告旁写一份同名 Markdown 摘要。若缺少 llama-server、GGUF/mmproj、CUDA、TensorRT engine 或遥测工具，相关阶段会记成 `not measured`，其他可运行阶段仍会执行。请勿把 RTX 工作站上的时间或独立模型的时间当成 Orin 整机数据。

## 看报告与判定

先检查每个 phase 的 `status`、`errors`、`latency.count`，再看 p50/p95 和 `resources`。JSON 保留每笔延迟，Markdown 供快速比较。identity 与 planning 的主要比较值是 `latency.p95_s`。YOLOE 各尺寸应同时看 p95、GPU 负载和 RAM；并行阶段要比较 identity 的 p95 是否明显恶化，并确认排程漏执行数 `missed_schedule_slots` 和实际重叠请求数 `overlapping_identity_calls`。

并行阶段使用 PyTorch fp16；可选 TensorRT 的数据属于单独的 YOLOE 阶段。默认每个尺寸有 8 张图 × 5 次重复，即 40 个延迟样本，identity 同样最多 40 个，planning 默认 15 个。p50 用中位数，p95 用 nearest-rank；样本少时 p95 接近最大值，应增加 `--repetitions` 再判断。

RAM、GPU 显存字段虽以 `_mb` 命名，按工具的 MiB 口径读取；RSS 在 JSON 中以 bytes 保存。Markdown 的 RSS 列是所有受监测进程中最大的单进程峰值，各进程详情见 `peak_rss_bytes_by_process`。不要把 RSS 相加当成统一内存总量，因为共享页和峰值时刻可能不同。遥测默认每 500 ms 采样一次，采样峰值可能漏过更短的瞬时尖峰；需要时用 `--tegrastats-interval-ms 100` 重跑。

初步验收门槛：

- identity p95 **低于 1.5 秒**。
- text-only planning p95 **低于 3 秒**。
- 任一阶段总 RAM 峰值 **低于 6.5 GB（十进制，约 6199 MiB）**，即 `peak_ram_used_mb < 6199`。用报告的 `ram_total_mb - peak_ram_used_mb` 检查实际余量，不要假定标称 8 GB 全部可用。`tegrastats` 报告统一内存，包含系统、相机和 GPU 共享分配；`peak_gpu_shared_memory_mb: null` 表示不能单独拆分 GPU 占用。
- 并行时 YOLOE 的漏排数应为 0 或很少，identity p95 不应大幅超出单独测试值。

任一门槛不过，先按以下顺序调整并重跑同一组数据：

1. planning 慢时先用 Q4_K_M GGUF 做对照，mmproj 保留 Q8_0。当前脚本用同一个 GGUF 测 identity 和 planning，仓库里的 `OnboardPolicy` 也复用同一个 llama-server；所以现有实现还不能只给 planner 换量化。把 `--gguf-model` 指向 Q4 文件重跑并对比两类请求；若要只让 planner 使用 Q4，需先给运行时增加独立的 planner 模型路由，再验证 identity 仍用 Q5_K_M 且视觉判断没有退化。
2. RAM 或 YOLOE 时延过高时，先降低 YOLOE `imgsz`（1280 降到 960，再到 640）。
3. YOLOE 仍无法跟上目标频率时，改用 Orin 上为相同模型/输入尺寸构建的 TensorRT engine。
4. 并行或 RAM 仍超标时，减少每个步骤的慢模型 identity 呼叫次数，并重新跑并行阶段确认。

6.5 GB 是保守的初始工程门槛，不是 JetPack 的硬件限制。判读时使用同一 Orin、电源模式、散热条件和后台服务状态；长时间运行仍须额外观察温度与降频。
