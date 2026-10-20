# Orin Nano v3 一条命令基准

`scripts/bench_orin_onboard.py` 支持在 Jetson 上顺序构建并测量 960/1280 两个 YOLOE TensorRT engine。构建时传入：

```bash
python3 scripts/bench_orin_onboard.py --build-engines \
  --proposer-onnx-960 artifacts/exports/orin-proposer/proposer-960.onnx \
  --proposer-onnx-1280 artifacts/exports/orin-proposer/proposer-1280.onnx \
  --tensorrt-engine-960 artifacts/exports/orin-proposer/proposer-960-fp16.engine \
  --tensorrt-engine-1280 artifacts/exports/orin-proposer/proposer-1280-fp16.engine \
  --depth-onnx artifacts/evaluation/depth-anything-v2-metric-outdoor-small.onnx \
  --depth-engine artifacts/exports/orin-depth/depth-anything-v2-small-fp16.engine
```

脚本会记录 TensorRT 快系统、r18 完整身份判断与 `fast_identity`、r14 规划对比和模型切换、默认开启的步间三角化 CPU、Depth Anything V2 Small、并行负载、tegrastats 内存和温度。`--build-engines` 按 960、1280、深度模型顺序运行 `trtexec --fp16 --memPoolSize=workspace:512 --skipInference`，engine 必须在目标 Jetson 的 TensorRT 环境中生成。

JSON 的 `budgets` 字段包含门槛、实测值、差距和结论：快系统 TensorRT P95 延迟对应频率至少 10 Hz；r18 `fast_identity` P95 不超过 1 秒；整机统一 RAM 峰值不超过 6199 MiB（约 6.5 GB 十进制）。缺少目标工具或模型时阶段为 `not measured`，不会伪造通过。同目录 Markdown 报告使用中文，列出每个阶段和预算结果。
