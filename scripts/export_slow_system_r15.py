#!/usr/bin/env python3
"""Execute the local HF merge -> F16 GGUF -> Q5_K_M conversion chain."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.convert_slow_system_lora import probe_local_tools  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", type=Path, required=True)
    p.add_argument("--adapter", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--llama-root", type=Path, default=ROOT / "artifacts/cache/llama-cpp")
    p.add_argument("--quantization", choices=("Q4_K_M", "Q5_K_M", "Q8_0"), default="Q5_K_M")
    a = p.parse_args()
    tools = probe_local_tools(a.llama_root)
    if not tools.convert_hf_to_gguf or not tools.llama_quantize:
        raise SystemExit("缺少本地 convert_hf_to_gguf.py 或 llama-quantize；未执行部分导出。")
    base = a.base
    if not (base / "config.json").is_file():
        snapshots = sorted(p.parent for p in base.glob("*/config.json") if p.is_file())
        if len(snapshots) != 1:
            raise SystemExit(f"无法唯一解析 base snapshot: {base}")
        base = snapshots[0]
    a.output.mkdir(parents=True, exist_ok=True)
    merged = a.output / "merged-hf"
    f16 = a.output / "slow-system-f16.gguf"
    quant = a.output / f"slow-system-{a.quantization}.gguf"
    merge_code = "\n".join((
        "from peft import PeftModel",
        "from transformers import AutoProcessor, Qwen3VLForConditionalGeneration",
        f"base = Qwen3VLForConditionalGeneration.from_pretrained({str(base)!r}, local_files_only=True, trust_remote_code=False, torch_dtype='auto')",
        f"model = PeftModel.from_pretrained(base, {str(a.adapter.resolve())!r}, is_trainable=False).merge_and_unload()",
        f"model.save_pretrained({str(merged.resolve())!r}, safe_serialization=True)",
        f"AutoProcessor.from_pretrained({str(base)!r}, local_files_only=True, trust_remote_code=False).save_pretrained({str(merged.resolve())!r})",
    ))
    merge_script = a.output / "merge.py"
    merge_script.write_text(merge_code + "\n", encoding="utf-8")
    subprocess.run([sys.executable, str(merge_script)], check=True, cwd=ROOT)
    subprocess.run([sys.executable, tools.convert_hf_to_gguf, str(merged), "--outfile", str(f16), "--outtype", "f16"], check=True, cwd=ROOT)
    subprocess.run([tools.llama_quantize, str(f16), str(quant), a.quantization], check=True, cwd=ROOT)
    print(quant)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
