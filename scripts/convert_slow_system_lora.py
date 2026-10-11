"""Plan the offline Qwen3-VL LoRA to GGUF conversion.

This helper never downloads files and never starts a GPU job.  It inspects the
checkout for llama.cpp/``gguf`` conversion helpers and emits a deterministic
plan for a later, explicitly requested conversion.  The cached b11552
llama.cpp distribution is a runtime-only build, so a plan normally reports
that conversion helpers are unavailable and prints the merge-then-convert
commands needed once a compatible local converter is supplied.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LLAMA_ROOT = ROOT / "artifacts" / "cache" / "llama-cpp"
DEFAULT_BASE = ROOT / "artifacts" / "cache" / "huggingface-vlm" / "models--Qwen--Qwen3-VL-2B-Instruct" / "snapshots"


@dataclass(frozen=True)
class LocalTools:
    llama_root: str
    convert_lora_to_gguf: str | None
    convert_hf_to_gguf: str | None
    gguf_python_module: str | None
    llama_quantize: str | None
    llama_server: str | None

    @property
    def adapter_conversion_available(self) -> bool:
        return bool(self.convert_lora_to_gguf)

    @property
    def hf_conversion_available(self) -> bool:
        return bool(self.convert_hf_to_gguf)


def _first_file(roots: list[Path], names: set[str]) -> Path | None:
    for root in roots:
        if not root.exists():
            continue
        if root.is_file() and root.name.casefold() in names:
            return root
        try:
            candidates = sorted(
                (p for p in root.rglob("*") if p.is_file() and p.name.casefold() in names),
                key=lambda p: str(p).casefold(),
            )
        except OSError:
            continue
        if candidates:
            return candidates[0]
    return None


def _module_origin(module_name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ModuleNotFoundError, ValueError):
        return None
    if spec is None or not spec.origin or spec.origin in {"built-in", "frozen"}:
        return None
    path = Path(spec.origin)
    return path if path.exists() else None


def probe_local_tools(llama_root: Path = DEFAULT_LLAMA_ROOT) -> LocalTools:
    """Discover only files already present locally; no network fallback."""

    roots = [llama_root]
    # A pip gguf package may ship the converter scripts beside its module.
    gguf_module = _module_origin("gguf")
    if gguf_module:
        roots.append(gguf_module.parent)
        # Some gguf wheels place console scripts in the distribution parent
        # rather than next to ``__init__.py``.
        roots.append(gguf_module.parent.parent)
    converter_lora = _first_file(roots, {"convert_lora_to_gguf.py"})
    converter_hf = _first_file(roots, {"convert_hf_to_gguf.py"})
    quantize = _first_file(
        roots,
        {"llama-quantize.exe", "llama-quantize", "llama-quantize.exe"},
    )
    server = _first_file(roots, {"llama-server.exe", "llama-server"})
    return LocalTools(
        llama_root=str(llama_root.resolve()),
        convert_lora_to_gguf=str(converter_lora.resolve()) if converter_lora else None,
        convert_hf_to_gguf=str(converter_hf.resolve()) if converter_hf else None,
        gguf_python_module=str(gguf_module.resolve()) if gguf_module else None,
        llama_quantize=str(quantize.resolve()) if quantize else None,
        llama_server=str(server.resolve()) if server else None,
    )


def _ps(path: Path | str) -> str:
    """PowerShell single-quoted literal (for the emitted reviewable plan)."""

    return "'" + str(path).replace("'", "''") + "'"


def conversion_plan(
    tools: LocalTools,
    *,
    base: Path = DEFAULT_BASE,
    adapter: Path | None = None,
    output: Path | None = None,
) -> dict:
    """Return commands and explicit availability facts without running them."""

    output = output or ROOT / "artifacts" / "models" / "slow-system-sft-r1-gguf"
    # Match the trainer's local snapshot resolution so the generated merge
    # command points at config.json rather than the parent cache directory.
    base_for_command = base
    if not (base / "config.json").is_file():
        snapshots = sorted(p.parent for p in base.glob("*/config.json") if p.is_file())
        if len(snapshots) == 1:
            base_for_command = snapshots[0]
    merged = output / "merged-hf"
    f16 = output / "slow-system-f16.gguf"
    q4 = output / "slow-system-Q4_K_M.gguf"
    adapter_text = _ps(adapter) if adapter else "<ADAPTER_DIR>"
    base_text = _ps(base)
    # Embed concrete paths in the emitted Python.  A single quoted
    # PowerShell here-string would leave ``$base`` literal inside Python,
    # which is an easy copy/paste failure on Windows.
    py_base = repr(str(base_for_command.resolve()))
    py_adapter = repr(str(adapter.resolve())) if adapter else "'<ADAPTER_DIR>'"
    py_merged = repr(str(merged.resolve()))
    commands = [
        "# GPU-free preparation is complete; run these only when the GPU is free.",
        f"$base = {base_text}",
        f"$adapter = {adapter_text}",
        f"$merged = {_ps(merged)}",
        '@"',
        "from pathlib import Path",
        "from peft import PeftModel",
        "from transformers import AutoProcessor, Qwen3VLForConditionalGeneration",
        f"base = Qwen3VLForConditionalGeneration.from_pretrained({py_base}, local_files_only=True, trust_remote_code=False, torch_dtype='auto')",
        f"model = PeftModel.from_pretrained(base, {py_adapter}, is_trainable=False).merge_and_unload()",
        f"model.save_pretrained({py_merged}, safe_serialization=True)",
        f"AutoProcessor.from_pretrained({py_base}, local_files_only=True, trust_remote_code=False).save_pretrained({py_merged})",
        '"@ | & .venv-vlm/Scripts/python.exe -',
    ]
    if tools.convert_hf_to_gguf:
        commands.append(
            f"& .venv-vlm/Scripts/python.exe {_ps(tools.convert_hf_to_gguf)} {_ps(merged)} --outfile {_ps(f16)} --outtype f16"
        )
    else:
        commands.append(
            "# MISSING: convert_hf_to_gguf.py; provide a local compatible llama.cpp converter before this step."
        )
    if tools.llama_quantize:
        commands.append(f"& {_ps(tools.llama_quantize)} {_ps(f16)} {_ps(q4)} Q4_K_M")
    else:
        commands.append("# MISSING: llama-quantize executable (runtime cache is incomplete).")
    return {
        "schema": "multidetect.slow-system-lora-conversion-plan-v1",
        "network_access": False,
        "gpu_work_performed": False,
        "base": str(base),
        "base_for_command": str(base_for_command),
        "adapter": str(adapter) if adapter else None,
        "output": str(output),
        "tools": asdict(tools),
        "adapter_conversion_available": tools.adapter_conversion_available,
        "hf_conversion_available": tools.hf_conversion_available,
        "commands": commands,
        "notes": [
            "llama-server accepts --lora PATH only for a GGUF LoRA adapter; an HF PEFT adapter cannot be passed directly.",
            "The language-only adapter leaves the existing Qwen3-VL mmproj GGUF unchanged.",
            "If vision projector modules were trained, regenerate a matching mmproj with the same converter build; do not mix versions.",
            "Do not quantize an already-quantized Q4 model; merge into HF, convert to F16 GGUF, then quantize once to Q4_K_M.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llama-root", type=Path, default=DEFAULT_LLAMA_ROOT)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--output-json", type=Path, help="Write the plan as UTF-8 LF JSON")
    args = parser.parse_args(argv)
    plan = conversion_plan(probe_local_tools(args.llama_root), base=args.base, adapter=args.adapter, output=args.output)
    rendered = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
