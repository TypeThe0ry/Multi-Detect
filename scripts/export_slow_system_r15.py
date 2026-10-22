#!/usr/bin/env python3
"""Execute the local HF merge -> F16 GGUF -> Q5_K_M conversion chain."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.convert_slow_system_lora import probe_local_tools  # noqa: E402

OWNER_MARKER = ".export-slow-system-owner.json"
LEDGER = "export-intermediates.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def claim_merged_dir(merged: Path, adapter: Path) -> None:
    """Mark merged-hf as this script's own output, or refuse a foreign directory.

    ``--drop-intermediates`` later deletes the directory, so it must never be one
    that already held something else (an existing model, another run's merge).
    """
    marker = merged / OWNER_MARKER
    if merged.exists() and not marker.is_file():
        raise SystemExit(
            f"{merged} 已存在且不是本导出脚本创建的；--drop-intermediates 拒绝覆盖或删除它"
        )
    merged.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"created_by": "export_slow_system_r15.py", "adapter": str(adapter)}) + "\n",
        encoding="utf-8",
    )


def drop_intermediate(output: Path, target: Path) -> None:
    """Record sizes and SHA-256 of this run's intermediate, then delete it."""
    if target.is_dir() and not (target / OWNER_MARKER).is_file():
        raise SystemExit(f"{target} 缺少本脚本的归属标记，拒绝删除")
    ledger_path = output / LEDGER
    ledger = (
        json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else {}
    )
    files = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
    entry = {
        p.relative_to(output).as_posix(): {"bytes": p.stat().st_size, "sha256": _sha256(p)}
        for p in files
    }
    ledger.setdefault("removed", {}).update(entry)
    tmp = ledger_path.with_name(LEDGER + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, ledger_path)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", type=Path, required=True)
    p.add_argument("--adapter", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--llama-root", type=Path, default=ROOT / "artifacts/cache/llama-cpp")
    p.add_argument("--quantization", choices=("Q4_K_M", "Q5_K_M", "Q8_0"), default="Q5_K_M")
    p.add_argument(
        "--drop-intermediates",
        action="store_true",
        help="hash, then delete this run's merged HF (after F16) and F16 GGUF (after quantization)",
    )
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
    if a.drop_intermediates:
        claim_merged_dir(merged, a.adapter)
    merge_code = "\n".join(
        (
            "from peft import PeftModel",
            "from transformers import AutoProcessor, Qwen3VLForConditionalGeneration",
            f"base = Qwen3VLForConditionalGeneration.from_pretrained({str(base)!r}, local_files_"
            "only=True, trust_remote_code=False, torch_dtype='auto')",
            f"model = PeftModel.from_pretrained(base, {str(a.adapter.resolve())!r}, is_"
            "trainable=False).merge_and_unload()",
            f"model.save_pretrained({str(merged.resolve())!r}, safe_serialization=True)",
            f"AutoProcessor.from_pretrained({str(base)!r}, local_files_only=True, trust_remote_"
            f"code=False).save_pretrained({str(merged.resolve())!r})",
        )
    )
    merge_script = a.output / "merge.py"
    merge_script.write_text(merge_code + "\n", encoding="utf-8")
    subprocess.run([sys.executable, str(merge_script)], check=True, cwd=ROOT)
    convert_env = dict(os.environ)
    if importlib.util.find_spec("sentencepiece") is None:
        # The Qwen converter imports sentencepiece before falling back to the BPE
        # tokenizer.json that Qwen3-VL uses; an import-only shim avoids installing it.
        shim = str(ROOT / "scripts" / "_conversion_shims")
        convert_env["PYTHONPATH"] = os.pathsep.join(
            p for p in (shim, convert_env.get("PYTHONPATH")) if p
        )
    subprocess.run(
        [
            sys.executable,
            tools.convert_hf_to_gguf,
            str(merged),
            "--outfile",
            str(f16),
            "--outtype",
            "f16",
        ],
        check=True,
        cwd=ROOT,
        env=convert_env,
    )
    if a.drop_intermediates:
        # The F16 GGUF now holds everything; dropping the merged HF weights before
        # quantizing keeps only F16 + quantized on disk for the last step.
        drop_intermediate(a.output, merged)
    subprocess.run(
        [tools.llama_quantize, str(f16), str(quant), a.quantization], check=True, cwd=ROOT
    )
    if a.drop_intermediates:
        drop_intermediate(a.output, f16)
        merge_script.unlink(missing_ok=True)
    print(quant)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
