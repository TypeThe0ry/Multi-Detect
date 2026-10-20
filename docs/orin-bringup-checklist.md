# Jetson Orin Nano 8GB 上板清单

这份清单针对 Jetson Orin Nano 8GB，目标是拿到板子后完成一次可复现的部署前实测。所有命令都在 Jetson 的无桌面终端执行；Windows 开发机只负责准备和校验文件。不要把 x86 机器生成的 TensorRT engine 带到板上，engine 必须在目标 JetPack/TensorRT 上生成。

## 0. 先准备拷贝包

从本 worktree 的 `tmp/orin-v3/` 拷贝下面两个 ONNX；从只读主仓库拷贝两个慢模型、mmproj、Depth Anything ONNX、源码和 20--50 张 PNG。文件复制后先核对 SHA-256：

| 文件 | 字节数 | SHA-256 |
|---|---:|---|
| `proposer-960.onnx` | 38,128,198 | `6f54d3fa397ff82d4d21edefbf4780c46319010a44cf31ce6a68758678ca374c` |
| `proposer-1280.onnx` | 38,422,200 | `09609336c91ae1b93287e7449aa876b84e03a2b9d846cbe237e1282c3dcf6864` |
| `slow-system-v2r18multi-merged-Q5_K_M.gguf`（2026-10-19 起机上身份模型） | 1,257,880,576 | `0326614baf72947f76c1c23ad038d9a15221d5d58c6f29cdc38bb257c924ba6e` |
| `slow-system-v2r14multi-merged-Q5_K_M.gguf` | 1,257,880,736 | `70cffdea752ce264bcccbace4d20b537b510091432e6823ef445393fb26f94ca` |
| `mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf` | 445,053,216 | `f9a68fabba69c3b81e153367b2c7521030b0fa8bb0de400c9599c8e6725f9c82` |
| `depth-anything-v2-metric-outdoor-small.onnx` | 99,156,661 | `36b38a0b34f5431a9bbcbedca7d72b440006204910d13227bc844641b0b23e52` |

默认身份模型是 r18，SHA-256 必须为上表值；r14 只用于明确的规划/对比阶段。步间三角化默认开启，`ground_height_sigma_m=5.0`。实际 hash 必须是无空格的 64 个十六进制字符。建议在 Windows 端用：

```powershell
Get-FileHash tmp/orin-v3/proposer-960.onnx -Algorithm SHA256
Get-FileHash tmp/orin-v3/proposer-1280.onnx -Algorithm SHA256
```

目标目录建议为 `/home/<user>/Multi-Detect`，目录内至少包含 `src/`、`scripts/`、`tmp/orin-v3/`（ONNX）、`artifacts/models/`（r18 身份 / r14 规划）、`artifacts/cache/`（mmproj）、`artifacts/evaluation/`（depth ONNX）和 `artifacts/recordings/orin-bench/png/`。身份 JSONL 若使用现有样本，复制到 `artifacts/datasets/slow-system-sft-v2-r4-prop/test.jsonl`，并保持其中 `image_path` 指向板上的相对路径。

## 1. 刷机和系统设置（预计 30--60 分钟）

1. 用 NVIDIA SDK Manager 刷 JetPack 6.2 或更新的 JetPack 6.x，选择无桌面/最小用户空间；确认设备型号为 Orin Nano 8GB。
2. 首次启动后设置正确 UTC 时间和主机名，接好主动散热和稳定电源。不要在风扇未工作的情况下跑长基准。
3. 设置最大性能模式并记录结果：

   ```bash
   sudo nvpmodel -m 0
   sudo jetson_clocks
   nvpmodel -q
   cat /proc/device-tree/model
   ```

4. 安装并运行仓库脚本（脚本会检查目标型号、安装板上已有的 JetPack runtime 依赖，并不会下载模型）：

   ```bash
   cd /home/<user>/Multi-Detect
   sudo -E bash scripts/bootstrap_jetson_orin.sh \
     --date-utc "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" \
     --app-dir /home/<user>/Multi-Detect
   ```

   预计 5--15 分钟。若目标网络不可用，先完成离线系统包安装和 Python venv，再按脚本错误安装同版本的 JetPack 系统包；不要在脚本失败后假装 benchmark 已完成。

## 2. 运行时自检（预计 2--5 分钟）

```bash
cd /home/<user>/Multi-Detect
source .venv/bin/activate
python -c 'import tensorrt, numpy, cv2, onnxruntime; print(tensorrt.__version__, numpy.__version__, cv2.__version__, onnxruntime.get_available_providers())'
command -v trtexec || test -x /usr/src/tensorrt/bin/trtexec
command -v llama-server
command -v tegrastats
sha256sum tmp/orin-v3/proposer-960.onnx tmp/orin-v3/proposer-1280.onnx \
  artifacts/models/slow-system-lora-v2-r18-multi-merged-gguf/slow-system-v2r18multi-merged-Q5_K_M.gguf \
  artifacts/models/slow-system-lora-v2-r14-multi-merged-gguf/slow-system-v2r14multi-merged-Q5_K_M.gguf \
  artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
  artifacts/evaluation/depth-anything-v2-metric-outdoor-small.onnx
```

常见失败：`tensorrt` 导入失败表示 JetPack runtime 没装完整；`llama-server` 找不到表示没有放入与板架构匹配的 llama.cpp 可执行文件；hash 不一致时删除目标副本并重新复制，不能继续测。

## 3. 一条命令构建并测量（预计 15--40 分钟）

下面的命令串行完成 960/1280 proposer engine 和 Depth Anything TensorRT engine 构建，然后测快系统、r18 full/fast identity、r14 planning 对比、模型切换、默认开启的步间三角化、近距深度、tegrastats 内存和温度，并写 JSON + 中文 Markdown：

```bash
cd /home/<user>/Multi-Detect
source .venv/bin/activate
python scripts/bench_orin_onboard.py \
  --build-engines \
  --proposer-onnx-960 tmp/orin-v3/proposer-960.onnx \
  --proposer-onnx-1280 tmp/orin-v3/proposer-1280.onnx \
  --tensorrt-engine-960 tmp/orin-v3/proposer-960-fp16.engine \
  --tensorrt-engine-1280 tmp/orin-v3/proposer-1280-fp16.engine \
  --depth-onnx artifacts/evaluation/depth-anything-v2-metric-outdoor-small.onnx \
  --depth-engine tmp/orin-v3/depth-anything-v2-small-fp16.engine \
  --frames-dir artifacts/recordings/orin-bench/png \
  --identity-jsonl artifacts/datasets/slow-system-sft-v2-r4-prop/test.jsonl \
  --gguf-model artifacts/models/slow-system-lora-v2-r18-multi-merged-gguf/slow-system-v2r18multi-merged-Q5_K_M.gguf \
  --r14-gguf-model artifacts/models/slow-system-lora-v2-r14-multi-merged-gguf/slow-system-v2r14multi-merged-Q5_K_M.gguf \
  --mmproj artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
  --frame-limit 20 --identity-limit 8 --repetitions 5 --warmup 1 \
  --concurrent-yolo-hz 10 --tegrastats-interval-ms 100 \
  --output artifacts/benchmarks/orin-v3.json \
  --markdown artifacts/benchmarks/orin-v3.md
```

`--concurrent-yolo-hz 10` 是预算验证目标；若实测 p95 低于 100 ms 才能通过快系统 10 Hz 门槛。报告中的门槛是：快系统至少 10 Hz、r18 fast identity p95 不超过 1 秒、整机 `tegrastats` 峰值 RAM 不超过 6199 MiB（约 6.5 GB 十进制）。报告会把缺失阶段标为 `not_measured`，不会把开发机时间当作 Orin 通过。

拿到板子的 IP 和用户名后，Windows 开发机只需执行这一条命令（默认使用 SSH 公钥）：

```powershell
$env:PYTHONPATH="src;."; python scripts/bringup_orin.py --host <板子IP> --user <用户名> --key $HOME/.ssh/id_ed25519
```

它会先把当前代码刷新进准备好的上板包 `D:\Projects\mdwt\orin-bundle\Multi-Detect`（模型、ONNX、llama.cpp 保持不变；换目录用 `--bundle`，不刷新代码用 `--no-refresh`），再用 `tar | ssh` 整包传到板子的 `~/Multi-Detect`，然后运行安装与 benchmark，把结果拉回 `artifacts/orin/<UTC时间>/`。先检查命令而不连接板子时，加 `--dry-run`。

前提：
- 开发机的公钥已经在板子的 `~/.ssh/authorized_keys` 里（只用密钥，不用密码）；第一次连接会自动记下板子的主机密钥。
- 安装脚本用 `sudo -n` 运行，不会弹出密码提示：板子上这个用户需要能免密 sudo，否则安装这一步会直接失败，可以在板子上手动执行 `sudo bash scripts/bootstrap_jetson_orin.sh ...` 后再用 `--no-refresh` 重跑。

## 4. 失败处理和复测

- engine 构建失败：保存 `engine_build` 中的命令和 stderr，先执行 `trtexec --help` 核对参数，再单独重跑对应 ONNX；不要复制其他 Jetson 型号生成的 engine。
- 内存超预算：先停止相机/桌面服务，确认只有一个 llama-server；再把 FAST 并发尺寸从 1280 改为 960，重跑同一组帧。不要通过减少 tegrastats 采样来掩盖峰值。
- r18/r14 模型切换失败：检查两个 GGUF hash、`llama-server` 架构和端口是否被占用；确保身份模型 server 已退出后再重试。
- 深度阶段失败：保留 ONNX provenance 和 TensorRT 日志，使用 `--skip-depth` 只做 B1 几何基线；B2 不能宣称通过，直到 Depth Anything engine 在板上成功加载并有耗时样本。
- 温度持续接近热限制或频率下降：检查散热和电源，记录 `tegrastats` 原始日志，冷却后在相同功耗模式复测。

最终归档 `orin-v3.json`、`orin-v3.md`、两个 engine 的 SHA-256、`engine_build` 日志、JetPack/TensorRT/llama.cpp 版本、`nvpmodel -q` 输出和原始 tegrastats 日志。
