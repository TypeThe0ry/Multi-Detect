# Slow-system LoRA to llama.cpp

This is an offline conversion note for `scripts/convert_slow_system_lora.py`.
The checkout's `artifacts/cache/llama-cpp/b11552` build contains
`llama-server.exe` and `llama-quantize.exe`, but no Python conversion scripts.
The `.venv-vlm` environment also has no `gguf` package.  The probe therefore
reports `convert_lora_to_gguf=null` and `convert_hf_to_gguf=null`; it does not
attempt a network install or download.

## Probe (CPU-only)

```powershell
.venv-vlm/Scripts/python.exe scripts/convert_slow_system_lora.py `
  --adapter artifacts/models/slow-system-sft-r1/adapter `
  --output-json artifacts/models/slow-system-sft-r1-conversion-plan.json
```

The command only inspects local files and writes a small JSON plan.  It does
not load model weights, allocate CUDA, or invoke a converter.

## Conversion when the GPU is free

Provide a compatible local `convert_hf_to_gguf.py` from the same llama.cpp
revision as the runtime.  Do not download it as part of this workflow.  Run
the generated plan's commands in order:

1. Load the local Qwen3-VL HF snapshot and the PEFT adapter, then call
   `merge_and_unload()` and save an unquantized HF directory.
2. Convert that merged directory to an F16 GGUF with `convert_hf_to_gguf.py`.
3. Quantize the F16 GGUF exactly once:

   ```powershell
   artifacts/cache/llama-cpp/b11552/llama-quantize.exe `
     artifacts/models/slow-system-sft-r1-gguf/slow-system-f16.gguf `
     artifacts/models/slow-system-sft-r1-gguf/slow-system-Q4_K_M.gguf Q4_K_M
   ```

The existing Qwen3-VL `mmproj-*.gguf` remains valid when the LoRA targets only
language q/v projections (the default trainer setting).  If
`--vision-projector` was used, regenerate a matching mmproj with the same
converter revision and evaluate it as a pair; do not mix projector revisions.
An HF PEFT adapter cannot be passed directly to llama.cpp.  `llama-server`
accepts `--lora PATH` only after a compatible GGUF LoRA adapter conversion.
The safe fallback is merged HF → F16 GGUF → Q4_K_M, as above.

## Local evaluator invocation

After conversion, the evaluator can use the existing cached server and the
same test prompt/schema:

```powershell
.venv-vlm/Scripts/python.exe scripts/eval_slow_system.py `
  --dataset artifacts/datasets/slow-system-sft-r1 `
  --backend gguf `
  --gguf-model artifacts/models/slow-system-sft-r1-gguf/slow-system-Q4_K_M.gguf `
  --mmproj artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf `
  --llama-server artifacts/cache/llama-cpp/b11552/llama-server.exe `
  --device cpu --output artifacts/models/slow-system-sft-r1-gguf/test-metrics.json
```

The command above is an evaluation recipe; the conversion and full-model
evaluation should wait until the RTX 4060 is free.  The unit tests exercise
tool probing and plan generation with temporary fake files only.
