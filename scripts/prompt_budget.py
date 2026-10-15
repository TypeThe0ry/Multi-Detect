"""Offline prompt accounting and aggregation of later prompt-budget GPU evaluations.

Only the Rust tokenizer, local JSON configuration and image headers are read.
No model weights, torch, CUDA, server or network client is loaded by accounting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

IDENTITY_VARIANTS = ("default", "focus_only", "frame_336", "crop_160", "compact")
PLAN_VARIANTS = ("default", "no_examples", "two_examples", "compact_rules", "compact")
MODEL_CACHE = "artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct"


def local_tokenizer_snapshot(path: Path) -> Path:
    """Accept a snapshot, model cache or snapshots directory without Hub fallback."""
    if (path / "tokenizer.json").is_file():
        return path.resolve()
    candidates = sorted(path.glob("*/tokenizer.json"))
    candidates += sorted(path.glob("snapshots/*/tokenizer.json"))
    if len(candidates) != 1:
        raise ValueError(f"Expected one local tokenizer under {path}; found {len(candidates)}")
    return candidates[0].parent.resolve()


class OfflineTokenizer:
    def __init__(self, snapshot: Path):
        from jinja2.sandbox import ImmutableSandboxedEnvironment
        from tokenizers import Tokenizer

        self.snapshot = local_tokenizer_snapshot(snapshot)
        self.tokenizer = Tokenizer.from_file(str(self.snapshot / "tokenizer.json"))
        config = json.loads((self.snapshot / "tokenizer_config.json").read_text("utf-8"))
        template_path = self.snapshot / "chat_template.json"
        template = (
            json.loads(template_path.read_text("utf-8"))["chat_template"]
            if template_path.is_file() else config["chat_template"]
        )
        env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
        self.template = env.from_string(template)
        self.preprocessor = json.loads(
            (self.snapshot / "preprocessor_config.json").read_text("utf-8")
        )

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def chat_count(self, prompt: str, images: int = 0) -> int:
        content = [{"type": "text", "text": prompt}]
        content += [{"type": "image"} for _ in range(images)]
        rendered = self.template.render(
            messages=[{"role": "user", "content": content}],
            add_generation_prompt=True, add_vision_id=False,
        )
        return self.count(rendered)

    def parts(self, parts: dict[str, str], separator: str = "\n") -> dict[str, int]:
        """Incremental prefix attribution sums exactly despite BPE boundaries."""
        result, prefix, previous = {}, "", 0
        for name, part in parts.items():
            if not part:
                result[name] = 0
                continue
            prefix += (separator if prefix else "") + part
            current = self.count(prefix)
            result[name], previous = current - previous, current
        return result


def image_grid(width: int, height: int, config: dict, max_pixels: int) -> dict[str, Any]:
    """Qwen2VL smart_resize from the installed HF processor, using local config.

    Qwen3-VL uses 16px patches and a 2x2 spatial merge. Images smaller than the
    configured 65,536px floor are enlarged, including 224px and 160px crops.
    """
    factor = config["patch_size"] * config["merge_size"]
    min_pixels = config["size"]["shortest_edge"]
    if min(width, height) <= 0 or max(width, height) / min(width, height) > 200:
        raise ValueError("Invalid image dimensions for Qwen processor")
    h, w = round(height / factor) * factor, round(width / factor) * factor
    if h * w > max_pixels:
        beta = math.sqrt(height * width / max_pixels)
        h = max(factor, math.floor(height / beta / factor) * factor)
        w = max(factor, math.floor(width / beta / factor) * factor)
    elif h * w < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h = math.ceil(height * beta / factor) * factor
        w = math.ceil(width * beta / factor) * factor
    return {
        "input_size": [width, height], "processor_size": [w, h],
        "visual_tokens": h * w // (factor * factor), "processed_pixels": h * w,
    }


def mean(values) -> float | None:
    values = [value for value in values if value is not None]
    return round(statistics.mean(values), 4) if values else None


def summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        raise ValueError("Cannot account for an empty evaluation set")
    keys = ("text_tokens", "chat_overhead_tokens", "visual_tokens", "prompt_tokens",
            "target_tokens", "processed_megapixels", "image_count")
    summary = {name: mean(row[name] for row in samples) for name in keys}
    summary["count"] = len(samples)
    summary["parts"] = {
        name: mean(row["parts"][name] for row in samples) for name in samples[0]["parts"]
    }
    summary["prompt_min"] = min(row["prompt_tokens"] for row in samples)
    summary["prompt_max"] = max(row["prompt_tokens"] for row in samples)
    return summary


def identity_parts(prompt: str) -> dict[str, str]:
    """Partition the existing prompt, retaining every byte and original order."""
    first, task, memory, *rest = prompt.split("\n")
    system, rules = first.split(" Choose", 1)
    # Same-line system/rules boundary is handled separately in account_identity.
    return {"system": system, "rules": " Choose" + rules, "task": task,
            "memory": memory, "refs": "\n".join(rest), "examples": ""}


def account_identity(dataset: Path, data_root: Path, tokenizer: OfflineTokenizer) -> list[dict]:
    from PIL import Image

    from scripts.slow_system_runtime import (
        identity_image_sizes,
        load_jsonl,
        prompt_for_row,
        zoom_candidates_for_row,
    )

    rows = load_jsonl(dataset / "test.jsonl", split="test")
    sizes = {}
    for row in rows:
        path = Path(row["image_path"])
        path = path if path.is_absolute() else data_root / path
        with Image.open(path) as source:
            sizes[row["sample_id"]] = source.size
    result = []
    for variant in IDENTITY_VARIANTS:
        samples, grid_counts = [], Counter()
        for original in rows:
            row = {**original, "_crop_policy": "candidate_zoom_v2"}
            prompt = prompt_for_row(row, identity_variant=variant)
            parts = identity_parts(prompt)
            # Prefix attribution without inserting a newline inside the first line.
            system = tokenizer.count(parts["system"])
            first_line = parts.pop("system") + parts.pop("rules")
            counts = tokenizer.parts({"instructions": first_line, **parts})
            counts = {"system": system, "rules": counts.pop("instructions") - system,
                      **counts}
            assert sum(counts.values()) == tokenizer.count(prompt)
            side, crop_side = identity_image_sizes(row, identity_variant=variant)
            width, height = sizes[row["sample_id"]]
            scale = min(1.0, side / max(width, height))
            full = round(width * scale), round(height * scale)
            image_sizes = [full] + [(crop_side, crop_side)] * len(
                zoom_candidates_for_row(row, variant)
            )
            grids = [image_grid(w, h, tokenizer.preprocessor, 448 * 448)
                     for w, h in image_sizes]
            for grid in grids:
                grid_counts[json.dumps(grid, sort_keys=True)] += 1
            visual = sum(grid["visual_tokens"] for grid in grids)
            # Rendered chat includes one image_pad placeholder per image; expand it.
            chat_tokens = tokenizer.chat_count(prompt, len(grids))
            text = tokenizer.count(prompt)
            samples.append({
                "sample_id": row["sample_id"], "parts": counts, "text_tokens": text,
                "chat_overhead_tokens": chat_tokens - text - len(grids),
                "visual_tokens": visual, "prompt_tokens": chat_tokens - len(grids) + visual,
                "target_tokens": tokenizer.count(json.dumps(
                    row["assistant_target"], ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )),
                "processed_megapixels": sum(g["processed_pixels"] for g in grids) / 1e6,
                "image_count": len(grids),
            })
        result.append({"suite": "identity", "dataset": dataset.name, "variant": variant,
                       **summarize_samples(samples), "images": [
                           {**json.loads(grid), "occurrences": count}
                           for grid, count in sorted(grid_counts.items())
                       ]})
    return result


def account_planning(tasks: Path, tokenizer: OfflineTokenizer) -> list[dict]:
    from multidetect.autonomy.mission_planner import plan_prompt, plan_prompt_parts

    document = json.loads(tasks.read_text("utf-8"))
    result = []
    for variant in PLAN_VARIANTS:
        samples = []
        for item in document["tasks"]:
            kwargs = {"refs": tuple(document.get("refs", ())), "prompt_variant": variant}
            parts = plan_prompt_parts(item["text"], **kwargs)
            prompt = plan_prompt(item["text"], **kwargs)
            counts = tokenizer.parts(parts, separator="")
            assert sum(counts.values()) == tokenizer.count(prompt)
            text, total = tokenizer.count(prompt), tokenizer.chat_count(prompt)
            samples.append({"parts": counts, "text_tokens": text, "prompt_tokens": total,
                            "chat_overhead_tokens": total - text, "visual_tokens": 0,
                            "target_tokens": None, "processed_megapixels": 0, "image_count": 0})
        result.append({"suite": "planning", "dataset": tasks.stem, "variant": variant,
                       **summarize_samples(samples)})
    return result


def accounting_markdown(rows: list[dict], encoder_ms_per_megapixel: float) -> str:
    lines = ["# Offline prompt budget (Qwen3-VL-2B local tokenizer)", "",
             "Means over every task/test row; counts include the chat template. "
             "Image tokens use the local HF resize configuration, not measured GGUF usage.", "",
             "| Suite / set | Variant | System | Rules | Examples | Refs | Task | "
             "Memory/repair | Chat | Images | Prompt total |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in rows:
        p = row["parts"]
        cells = [row["suite"] + "/" + row["dataset"], row["variant"]]
        cells += [f"{p.get(key, 0):.1f}" for key in
                  ("system", "rules", "examples", "refs", "task")]
        cells += [f"{p.get('memory', 0) + p.get('repair', 0):.1f}"]
        cells += [f"{row[key]:.1f}" for key in
                  ("chat_overhead_tokens", "visual_tokens", "prompt_tokens")]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "## Orin Nano latency scenarios", "",
              "Prefill = prompt tokens / 1,400; decode = output tokens / 35. "
              "Planning output uses the supplied 90–150-token range; identity output uses "
              "the tokenized reference answer mean (actual generations may differ).",
              f"Image encoder scenario: {encoder_ms_per_megapixel:g} ms per processed MP "
              "(unmeasured assumption; use --encoder-ms-per-megapixel to replace it). "
              "This linear scenario omits fixed per-image, transfer and host overhead.", "",
              "| Suite / set | Variant | Prefill s | Decode s | Image MP | Encoder s "
              "(scenario) | Total s (scenario) |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        prefill = row["prompt_tokens"] / 1400
        low, high = ((90 / 35, 150 / 35) if row["suite"] == "planning"
                     else (row["target_tokens"] / 35,) * 2)
        encoder = row["processed_megapixels"] * encoder_ms_per_megapixel / 1000
        decode = f"{low:.3f}–{high:.3f}" if low != high else f"{low:.3f}"
        total = (f"{prefill + low + encoder:.3f}–{prefill + high + encoder:.3f}"
                 if low != high else f"{prefill + low + encoder:.3f}")
        lines.append(f"| {row['suite']}/{row['dataset']} | {row['variant']} | "
                     f"{prefill:.3f} | {decode} | {row['processed_megapixels']:.3f} | "
                     f"{encoder:.3f} | {total} |")
    lines += ["", "## Image sizes", "",
              "| Input W×H | HF W×H | Visual tokens / image |",
              "|---|---|---:|"]
    grids = {tuple(g["input_size"]): g for row in rows for g in row.get("images", [])}
    for size, grid in sorted(grids.items()):
        w, h = grid["processor_size"]
        lines.append(f"| {size[0]}×{size[1]} | {w}×{h} | {grid['visual_tokens']} |")
    return "\n".join(lines) + "\n"


def collect_evaluations(directory: Path) -> str:
    """Join real accuracy, server token usage and wall time; never invent usage."""
    lines = ["# Prompt budget GPU evaluation", "",
             "Accuracy is identity match/mismatch accuracy or exact planning skill sequence. "
             "Prompt tokens are actual backend usage (including visual tokens); n/a means "
             "the backend did not report them. Prompt mean is the first request; prompt total "
             "and output mean include planning retries. Latency is per request/task wall time, "
             "including planning retries; model startup is excluded. The matrix runner "
             "disables prompt caching; manual reports must use the same option.",
             "", "| Suite | Set | Variant | N | Accuracy | Valid | Prompt tokens mean | "
             "Output tokens mean | Latency p50 s | Latency p95 s | Prompt total mean | "
             "Prompt usage N |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    files = sorted(directory.glob("identity--*--*.json"))
    files += sorted(directory.glob("planning--*--*.json"))
    if not files:
        raise ValueError(f"No evaluation reports found under {directory}")
    for path in files:
        suite, dataset, variant = path.stem.split("--")
        data = json.loads(path.read_text("utf-8"))
        rows = data["rows"]
        if not rows:
            raise ValueError(f"Empty evaluation: {path}")
        prompt = mean(row.get("prompt_tokens") for row in rows)
        prompt_total = mean(row.get("prompt_tokens_total", row.get("prompt_tokens"))
                            for row in rows)
        completion = mean(row.get("completion_tokens", row.get("completion_tokens_total"))
                          for row in rows)
        if suite == "identity":
            metrics = data["metrics"]
            accuracy, valid = metrics["identity_accuracy"], metrics["json_validity"]
            latency = metrics["latency_s"]["p50"]
            p95 = metrics["latency_s"].get("p95")
        else:
            metrics = data["summary"]
            accuracy, valid = metrics["exact_sequence_rate"], metrics["valid_rate"]
            latency = metrics["median_latency_s"]
            latencies = sorted(sum(row["attempts_s"]) for row in rows if "attempts_s" in row)
            p95 = latencies[math.ceil((len(latencies) - 1) * 0.95)] if latencies else None
        fields = [suite, dataset, variant, str(len(rows))]
        fields += [f"{value:.1%}" if value is not None else "n/a"
                   for value in (accuracy, valid)]
        fields += [f"{value:.2f}" if value is not None else "n/a"
                   for value in (prompt, completion, latency, p95, prompt_total)]
        fields.append(f"{sum(row.get('prompt_tokens') is not None for row in rows)}/{len(rows)}")
        lines.append("| " + " | ".join(fields) + " |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    account = commands.add_parser("account", help="CPU-only; offline; no model loading")
    account.add_argument("--data-root", type=Path, default=ROOT)
    account.add_argument("--tokenizer", type=Path)
    account.add_argument("--output-dir", type=Path, required=True)
    account.add_argument("--encoder-ms-per-megapixel", type=float, default=1000)
    collect = commands.add_parser("collect")
    collect.add_argument("--input-dir", type=Path, required=True)
    collect.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "collect":
        content = collect_evaluations(args.input_dir)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    else:
        if not math.isfinite(args.encoder_ms_per_megapixel) or args.encoder_ms_per_megapixel < 0:
            parser.error("--encoder-ms-per-megapixel must be finite and nonnegative")
        tokenizer = OfflineTokenizer(args.tokenizer or args.data_root / MODEL_CACHE)
        rows = []
        for name in ("slow-system-sft-v2-r4-prop", "flight-identity-eval-v1"):
            rows += account_identity(args.data_root / "artifacts/datasets" / name,
                                     args.data_root, tokenizer)
        for name in ("plan-eval-tasks.json", "plan-eval-tasks-v2.json"):
            rows += account_planning(ROOT / "configs/onboard" / name, tokenizer)
        content = accounting_markdown(rows, args.encoder_ms_per_megapixel)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        sources = [args.data_root / "artifacts/datasets" / name / "test.jsonl"
                   for name in ("slow-system-sft-v2-r4-prop", "flight-identity-eval-v1")]
        sources += [ROOT / "configs/onboard" / name
                    for name in ("plan-eval-tasks.json", "plan-eval-tasks-v2.json")]
        provenance = {"tokenizer": str(tokenizer.snapshot), "files_sha256": {
            name: hashlib.sha256((tokenizer.snapshot / name).read_bytes()).hexdigest()
            for name in ("tokenizer.json", "tokenizer_config.json", "chat_template.json",
                         "preprocessor_config.json")
            if (tokenizer.snapshot / name).is_file()
        }, "input_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in sources},
            "image_backend": "HF local preprocessor configuration; GGUF usage measured later",
            "max_pixels": 448 * 448,
            "encoder_ms_per_megapixel_scenario": args.encoder_ms_per_megapixel, "rows": rows}
        (args.output_dir / "token-accounting.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        (args.output_dir / "token-accounting.md").write_text(content, encoding="utf-8")
    print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
