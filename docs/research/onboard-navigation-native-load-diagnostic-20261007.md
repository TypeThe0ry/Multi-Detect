# 导航 pilot：原生加载失败诊断（2026-10-07）

结论：原生访问违规有证据；具体根因未证实，不能声称 OOM、训练完成或依赖升级即修复。

## 实际失败证据

- 原件：[`invocation.json`](../../artifacts/models/onboard-vlm-navigation-pilot-20261007-r1/invocation.json)、[`native-exit-observation.json`](../../artifacts/models/onboard-vlm-navigation-pilot-20261007-r1/native-exit-observation.json)。session 20379 原生工具退出码为 1；这不是 Windows exception code。
- 最后可见的局部 stdout：`navigation_pilot_before_model_load`，RTX 4060；随后是 `torch_dtype` 弃用警告。没有完整进程日志或 Python traceback。
- Windows Application Event 1000，record 267856，2026-10-07 18:27:09.132334 +08:00：`python.exe` PID 22856，`torch_cpu.dll`，`0xc0000005`，offset `0x6046edb`；Report ID `7ebb357a-5c1e-4aa4-8b1f-69ba2ab80d0f`。关联依据是时间、运行时路径、模块与加载阶段 stdout；原工具未提供 child PID，因此不能称精确 PID 配对。
- `report.json`、prepared inputs、训练事件和 adapter 均缺失。optimizer attempted/completed/performed **未知（null），不是零**。不补造原训练报告，不把初始化失败推断当作完整执行审计。
- 普通 Python Exception 会进入 trainer 收尾；原生访问违规可能绕过 `except Exception`，与缺失报告一致。

## 静态加载链与候选原因

`.venv-vlm/pyvenv.cfg` 开启 system site packages：本地 Transformers 4.57.6 / Accelerate 1.12.0 / PEFT 0.17.1，继承全局 Torch 2.6.0 + cu124、safetensors 0.4.5。0.4.5 满足 Transformers 声明的 `>=0.4.3`，不能称依赖不满足。

现有 backend：processor → `Qwen3VLForConditionalGeneration.from_pretrained`（本地 BF16、SDPA、CUDA）→ LoRA → 参数摘要。本地 Transformers 的 safetensors 路径是 `safe_open(..., framework="pt", device="cuda:0") → get_slice → param[...]`。

[safetensors v0.4.5 Rust 源码](https://github.com/safetensors/safetensors/blob/v0.4.5/bindings/python/src/lib.rs#L424-L460) 对 PT 使用 `torch.UntypedStorage.from_file(shared=False, nbytes=whole file)`；[切片路径](https://github.com/safetensors/safetensors/blob/v0.4.5/bindings/python/src/lib.rs#L914-L945) 再做 storage slice → `torch.asarray(uint8)` → view(dtype)。Windows 文件映射／Storage／Rust 交接值得优先核验，但尚无本机调用栈证明。

[PyTorch #145864 的诊断评论](https://github.com/pytorch/pytorch/issues/145864#issuecomment-3630988973) 提出映射指针交接失效；[safetensors #693](https://github.com/safetensors/safetensors/issues/693) 在 0.6.2 仍报告类似访问违规。相似上游报告不是本机根因证明；不能按 DLL offset 或版本号直接认定，更不能升级后未经复现就称修好。

资源压力是时变假设：诊断期间主机空闲 RAM/virtual 曾约 0.99/1.08 GiB，后来约 8.3/9.4 GiB；不是崩溃瞬间的精确测量。加载前 GPU free 7440695296 B、total 8585216000 B；PyTorch reserved cap 与全设备总占用不同。没有普通 CUDA OOM traceback，**OOM 未证实**。CPU 多线程也缺少匹配证据。

## 下一步（尚未验收）

用新隔离进程做 load-only `faulthandler` 诊断，强制刷新阶段日志，由外部 supervisor 捕捉退出码与 Windows 事件；不创建 optimizer。若栈落 Storage/safetensors，再分段测元数据及极小 CPU slice；若落模型初始化／allocator warmup，沿该分支调查。保持原失败、封存计划与预算不变；不自动升级、改 dtype、量化/offload、重训或停止其他进程。

本记录仅诊断，未运行 GPU；不产生质量、promotion、HIL/REAL、室外 GNSS 或最终发布通过声明。
