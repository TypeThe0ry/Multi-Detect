"""Evaluate slow-system decisions on the held-out test split.

The HF and llama.cpp paths consume the same rendered prompt/image helper. No
truth fields are sent to either model: truth is read only from assistant_target
to compute metrics after inference.
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multidetect.autonomy.onboard_decision import DECISION_JSON_SCHEMA, parse_onboard_decision
from scripts.slow_system_runtime import ROOT, chat_messages, image_jpeg_bytes, load_jsonl, prompt_for_row
from scripts.train_slow_system_lora import local_snapshot

DEFAULT_DATASET = ROOT / "artifacts/datasets/slow-system-sft-r1"
DEFAULT_CACHE = ROOT / "artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots"
DEFAULT_LLAMA = ROOT / "artifacts/cache/llama-cpp/b11552/llama-server.exe"


def parse_model_decision(raw: str | bytes | dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = parse_onboard_decision(raw)
        return {
            "decision": value.decision,
            "args": {"track_id": value.args.track_id, "region": value.args.region, "duration_s": value.args.duration_s},
            "reason": value.reason,
        }, None
    except Exception as exc:
        # Models occasionally wrap JSON in a fenced block. Accept only a single
        # object after removing that presentation wrapper; schema parsing remains strict.
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                value = parse_onboard_decision(text[start : end + 1])
                return {
                    "decision": value.decision,
                    "args": {"track_id": value.args.track_id, "region": value.args.region, "duration_s": value.args.duration_s},
                    "reason": value.reason,
                }, None
            except Exception:
                pass
        return None, f"{type(exc).__name__}: {exc}"


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * quantile + 0.999999))], 4)


def decision_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in results if row["valid"]]
    expected = [str(row["expected"]) for row in results]
    predictions = [str(row.get("decision")) for row in results]
    confirms = [row for row in results if row["expected"] == "confirm_target"]
    rejects = [row for row in results if row["expected"] == "reject_track"]
    predicted_confirm = [row for row in results if row.get("decision") == "confirm_target"]
    correct = sum(row.get("decision") == row["expected"] for row in results)
    true_confirm = sum(row.get("decision") == "confirm_target" for row in confirms)
    correct_reject = sum(row.get("decision") == "reject_track" for row in rejects)
    return {
        "count": len(results), "valid_json_count": len(valid),
        "json_validity": len(valid) / len(results) if results else None,
        "decision_correct_count": correct,
        "decision_accuracy": correct / len(results) if results else None,
        "confirm_precision": true_confirm / len(predicted_confirm) if predicted_confirm else None,
        "confirm_recall": true_confirm / len(confirms) if confirms else None,
        "distractor_rejection_count": correct_reject,
        "distractor_rejection_rate": correct_reject / len(rejects) if rejects else None,
        "invalid_count": len(results) - len(valid),
        "expected_counts": {value: expected.count(value) for value in sorted(set(expected))},
        "predicted_counts": {value: predictions.count(value) for value in sorted(set(predictions))},
        "latency_s": {"p50": _percentile([float(row["latency_s"]) for row in results], 0.5),
                      "p95": _percentile([float(row["latency_s"]) for row in results], 0.95)},
    }


def evaluate_predictor(rows: list[dict[str, Any]], predictor: Callable[[dict[str, Any]], tuple[str, float]]) -> dict[str, Any]:
    results = []
    for row in rows:
        started = time.perf_counter()
        try:
            prediction = predictor(row)
            if isinstance(prediction, tuple):
                raw, reported_latency = prediction
            else:
                raw, reported_latency = prediction, None
            parsed, failure = parse_model_decision(raw)
            elapsed = float(reported_latency) if reported_latency is not None else time.perf_counter() - started
            result = {
                "sample_id": row["sample_id"], "world": row["world"], "expected": row["assistant_target"]["decision"],
                "raw_output": raw, "latency_s": elapsed, "valid": parsed is not None,
                "decision": parsed["decision"] if parsed else None, "parsed_output": parsed, "failure": failure,
            }
        except Exception as exc:
            result = {
                "sample_id": row["sample_id"], "world": row["world"], "expected": row["assistant_target"]["decision"],
                "raw_output": None, "latency_s": time.perf_counter() - started, "valid": False,
                "decision": None, "parsed_output": None, "failure": f"{type(exc).__name__}: {exc}",
            }
        results.append(result)
    return {"metrics": decision_metrics(results), "rows": results}


class HFRunner:
    def __init__(self, model_path: Path, adapter: Path | None, *, device: str = "cuda:0", max_side: int = 448, max_new_tokens: int = 128):
        import torch
        import transformers

        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("HF evaluation requested CUDA but CUDA is unavailable")
        snapshot = local_snapshot(model_path)
        self.torch, self.max_new_tokens, self.max_side = torch, max_new_tokens, max_side
        self.processor = transformers.AutoProcessor.from_pretrained(str(snapshot), local_files_only=True, trust_remote_code=False, max_pixels=max_side * max_side)
        self.model = transformers.Qwen3VLForConditionalGeneration.from_pretrained(
            str(snapshot), local_files_only=True, trust_remote_code=False,
            torch_dtype=torch.bfloat16 if device.startswith("cuda") else torch.float32,
            attn_implementation="sdpa", device_map={"": device},
        ).eval()
        self.adapter = str(adapter) if adapter else None
        if adapter:
            import peft
            self.model = peft.PeftModel.from_pretrained(self.model, str(adapter), is_trainable=False).eval()

    def __call__(self, row: dict[str, Any]) -> tuple[str, float]:
        torch = self.torch
        inputs = self.processor.apply_chat_template(
            chat_messages(row, self.max_side), tokenize=True, add_generation_prompt=True,
            return_dict=True, return_tensors="pt",
        ).to(next(self.model.parameters()).device)
        started = time.perf_counter()
        with torch.inference_mode():
            output = self.model.generate(**inputs, do_sample=False, max_new_tokens=self.max_new_tokens, use_cache=True)
        elapsed = time.perf_counter() - started
        input_tokens = inputs["input_ids"].shape[-1]
        return self.processor.tokenizer.decode(output[0, input_tokens:].tolist(), skip_special_tokens=True, clean_up_tokenization_spaces=False), elapsed


def _http_json(url: str, body: dict[str, Any] | None = None, timeout: float = 180) -> dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class LlamaServerRunner:
    def __init__(self, model: Path, mmproj: Path, *, executable: Path = DEFAULT_LLAMA, adapter: Path | None = None, port: int = 9933, context: int = 4096, max_new_tokens: int = 128, device: str = "cuda:0"):
        self.port, self.max_new_tokens = port, max_new_tokens
        if device == "cpu":
            gpu_layers = "0"
        elif device.startswith("cuda"):
            gpu_layers = "all"
        else:
            raise ValueError("GGUF device must be cpu or cuda[:index]")
        command = [str(executable), "-m", str(model), "-mm", str(mmproj), "-ngl", gpu_layers, "-c", str(context), "-n", str(max_new_tokens), "-np", "1", "--host", "127.0.0.1", "--port", str(port), "--no-webui", "--cache-prompt", "--cache-reuse", "64"]
        if adapter:
            command.extend(["--lora", str(adapter)])
        self.process = subprocess.Popen(command, cwd=str(executable.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"llama-server exited with {self.process.returncode}")
            try:
                if _http_json(f"http://127.0.0.1:{port}/health", timeout=2).get("status") == "ok":
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(1)
        self.close()
        raise TimeoutError("llama-server did not become healthy")

    def __call__(self, row: dict[str, Any]) -> tuple[str, float]:
        image = "data:image/jpeg;base64," + base64.b64encode(image_jpeg_bytes(row)).decode("ascii")
        body = {
            "model": "local", "temperature": 0, "max_tokens": self.max_new_tokens, "stream": False,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt_for_row(row)}, {"type": "image_url", "image_url": {"url": image}}]}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "onboard_decision", "strict": True, "schema": DECISION_JSON_SCHEMA}},
        }
        started = time.perf_counter()
        response = _http_json(f"http://127.0.0.1:{self.port}/v1/chat/completions", body)
        elapsed = time.perf_counter() - started
        return response["choices"][0]["message"].get("content", ""), elapsed

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


def run(args) -> dict[str, Any]:
    rows = load_jsonl(args.dataset / "test.jsonl", split="test")
    if args.backend == "fake":
        predictor = lambda row: (json.dumps(row["assistant_target"], separators=(",", ":")), 0.001)
        return evaluate_predictor(rows, predictor)
    runner = None
    try:
        if args.backend == "hf":
            runner = HFRunner(args.model, args.adapter, device=args.device, max_side=args.max_side, max_new_tokens=args.max_new_tokens)
        else:
            if not args.gguf_model or not args.mmproj:
                raise ValueError("GGUF backend needs --gguf-model and --mmproj")
            runner = LlamaServerRunner(args.gguf_model, args.mmproj, executable=args.llama_server, adapter=args.adapter, port=args.port, context=args.context, max_new_tokens=args.max_new_tokens, device=args.device)
        return evaluate_predictor(rows, runner)
    finally:
        if isinstance(runner, LlamaServerRunner):
            runner.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--backend", choices=("hf", "gguf", "fake"), default="hf")
    parser.add_argument("--model", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--max-side", type=int, default=448)
    parser.add_argument("--max-new-tokens", type=int, default=128)
    parser.add_argument("--gguf-model", type=Path)
    parser.add_argument("--mmproj", type=Path)
    parser.add_argument("--llama-server", type=Path, default=DEFAULT_LLAMA)
    parser.add_argument("--port", type=int, default=9933)
    parser.add_argument("--context", type=int, default=4096)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = run(args)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8", newline="\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
