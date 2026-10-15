"""Evaluate slow-system decisions on the held-out test split.

The HF and llama.cpp paths consume the same rendered prompt/image helper. No
truth fields are sent to either model: truth is read only from assistant_target
to compute metrics after inference.
"""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import base64
import json
import math
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multidetect.autonomy.onboard_decision import (
    DECISION_JSON_SCHEMA,
    IDENTITY_DECISIONS,
    identity_decision_schema,
    identity_of,
    parse_onboard_decision,
)
from scripts.slow_system_runtime import (
    IDENTITY_VARIANTS,
    ROOT,
    chat_messages,
    dataset_crop_policy,
    image_jpeg_bytes_list,
    load_jsonl,
    prompt_for_row,
    trained_key_order_schema,
)
from scripts.train_slow_system_lora import local_snapshot

DEFAULT_DATASET = ROOT / "artifacts/datasets/slow-system-sft-r1"
DEFAULT_CACHE = ROOT / "artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots"
DEFAULT_LLAMA = ROOT / "artifacts/cache/llama-cpp/b11552/llama-server.exe"


def decision_confidence(tokens: list[dict[str, Any]] | None) -> float | None:
    """Probability of the first token of the decision value, from llama.cpp logprobs.

    Keys are emitted in sorted order (args before decision), so the value is the
    first token generated after the text '"decision":"'.
    """
    if not tokens:
        return None
    text = ""
    for item in tokens:
        if text.rstrip().endswith('"decision":"') or text.endswith('"decision": "'):
            logprob = item.get("logprob")
            return None if logprob is None else float(math.exp(float(logprob)))
        text += str(item.get("token", ""))
    return None


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


def _token_summary(results: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [float(row[key]) for row in results if row.get(key) is not None]
    return {"count": len(values), "mean": sum(values) / len(values) if values else None,
            "p50": _percentile(values, 0.5), "p95": _percentile(values, 0.95)}


def _usage_count(usage: Any, key: str) -> int | None:
    value = usage.get(key) if isinstance(usage, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return int(value)
    return None


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
    # Identity view: approach/confirm both mean "this is the task's object"; the host
    # applies the size gate (gate_confirm_by_size) to choose between them.
    identity_rows = [row for row in results if identity_of(str(row["expected"]))]
    identity_ok = sum(identity_of(str(row.get("decision"))) == identity_of(str(row["expected"])) for row in identity_rows)
    matches = [row for row in identity_rows if identity_of(str(row["expected"])) == "match"]
    predicted_matches = [row for row in identity_rows if identity_of(str(row.get("decision"))) == "match"]
    true_matches = sum(identity_of(str(row.get("decision"))) == "match" for row in matches)
    return {
        "identity_accuracy": identity_ok / len(identity_rows) if identity_rows else None,
        "identity_match_recall": true_matches / len(matches) if matches else None,
        "identity_match_precision": true_matches / len(predicted_matches) if predicted_matches else None,
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
        "prompt_tokens": _token_summary(results, "prompt_tokens"),
        "completion_tokens": _token_summary(results, "completion_tokens"),
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
                "prompt_tokens": _usage_count(getattr(predictor, "last_usage", None), "prompt_tokens"),
                "completion_tokens": _usage_count(getattr(predictor, "last_usage", None), "completion_tokens"),
            }
        except Exception as exc:
            result = {
                "sample_id": row["sample_id"], "world": row["world"], "expected": row["assistant_target"]["decision"],
                "raw_output": None, "latency_s": time.perf_counter() - started, "valid": False,
                "decision": None, "parsed_output": None, "failure": f"{type(exc).__name__}: {exc}",
                "prompt_tokens": None, "completion_tokens": None,
            }
        results.append(result)
    return {"metrics": decision_metrics(results), "rows": results}


class HFRunner:
    def __init__(self, model_path: Path, adapter: Path | None, *, device: str = "cuda:0", max_side: int = 448, max_new_tokens: int = 128, crop_policy: str = "none", identity_variant: str | None = None):
        import torch
        import transformers

        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("HF evaluation requested CUDA but CUDA is unavailable")
        snapshot = local_snapshot(model_path)
        self.torch, self.max_new_tokens, self.max_side, self.crop_policy = torch, max_new_tokens, max_side, crop_policy
        self.identity_variant, self.last_usage = identity_variant, None
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
        self.last_usage = None
        torch = self.torch
        inputs = self.processor.apply_chat_template(
            chat_messages(row, self.max_side, self.crop_policy, identity_variant=self.identity_variant), tokenize=True, add_generation_prompt=True,
            return_dict=True, return_tensors="pt",
        ).to(next(self.model.parameters()).device)
        started = time.perf_counter()
        with torch.inference_mode():
            output = self.model.generate(**inputs, do_sample=False, max_new_tokens=self.max_new_tokens, use_cache=True)
        elapsed = time.perf_counter() - started
        input_tokens = inputs["input_ids"].shape[-1]
        self.last_usage = {"prompt_tokens": input_tokens, "completion_tokens": output.shape[-1] - input_tokens}
        return self.processor.tokenizer.decode(output[0, input_tokens:].tolist(), skip_special_tokens=True, clean_up_tokenization_spaces=False), elapsed


def _http_json(url: str, body: dict[str, Any] | None = None, timeout: float = 180) -> dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class LlamaServerRunner:
    fast_identity = False  # tests build bare instances

    def __init__(self, model: Path, mmproj: Path, *, executable: Path = DEFAULT_LLAMA, adapter: Path | None = None, port: int = 9933, context: int = 4096, max_new_tokens: int = 128, device: str = "cuda:0", crop_policy: str = "none", max_side: int = 448, identity_only: bool = False, logprobs: bool = False, fast_identity: bool = False, identity_variant: str | None = None, cache_prompt: bool = True, call_timeout_s: float = 20.0):
        self.port, self.max_new_tokens, self.crop_policy, self.max_side = port, max_new_tokens, crop_policy, max_side
        self.call_timeout_s = call_timeout_s
        # Identity rows: prefill the pinned args, read one token's log-probabilities
        # for the three verdicts instead of decoding the whole JSON (~50 tokens).
        self.fast_identity = fast_identity and identity_only
        self._verdict_tokens: dict[str, dict[str, int]] = {}
        self._verdict_pieces: dict[str, dict[str, str | None]] = {}
        self.identity_variant, self.cache_prompt, self.last_usage = identity_variant, cache_prompt, None
        # Rows that name a focus track only allow identity verdicts (constrained decoding).
        self.identity_only = identity_only
        # Optionally keep token log-probabilities of the last answer (decision confidence).
        self.logprobs, self.last_logprobs = logprobs, None
        if device == "cpu":
            gpu_layers = "0"
        elif device.startswith("cuda"):
            gpu_layers = "all"
        else:
            raise ValueError("GGUF device must be cpu or cuda[:index]")
        # llama-server runs with cwd=executable.parent, so relative paths must be resolved first.
        executable, model, mmproj = Path(executable).resolve(), Path(model).resolve(), Path(mmproj).resolve()
        adapter = Path(adapter).resolve() if adapter else None
        command = [str(executable), "-m", str(model), "-mm", str(mmproj), "-ngl", gpu_layers, "-c", str(context), "-n", str(max_new_tokens), "-np", "1", "--host", "127.0.0.1", "--port", str(port), "--no-webui", "--cache-prompt", "--cache-reuse", "64"]
        if adapter:
            command.extend(["--lora", str(adapter)])
        self._command, self._cwd = command, str(executable.parent)
        self.server_log = tempfile.NamedTemporaryFile(prefix="llama-server-", suffix=".log", delete=False)
        self.process = subprocess.Popen(self._command, cwd=self._cwd, stdout=subprocess.DEVNULL, stderr=self.server_log)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.server_log.flush()
                tail = Path(self.server_log.name).read_text(encoding="utf-8", errors="replace")[-2000:]
                raise RuntimeError(f"llama-server exited with {self.process.returncode}: {tail}")
            try:
                if _http_json(f"http://127.0.0.1:{port}/health", timeout=2).get("status") == "ok":
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(1)
        self.close()
        raise TimeoutError("llama-server did not become healthy")

    def health_check(self, *, timeout: float = 2.0) -> bool:
        """Bounded health probe used by the onboard degradation monitor."""
        if self.process.poll() is not None:
            return False
        try:
            return _http_json(f"http://127.0.0.1:{self.port}/health", timeout=timeout).get("status") == "ok"
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def restart(self) -> None:
        self.close()
        self.server_log = tempfile.NamedTemporaryFile(prefix="llama-server-", suffix=".log", delete=False)
        self.process = subprocess.Popen(self._command, cwd=self._cwd, stdout=subprocess.DEVNULL, stderr=self.server_log)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.health_check(timeout=1):
                return
            time.sleep(0.25)
        self.close()
        raise TimeoutError("llama-server restart did not become healthy")

    def _verdict_first_tokens(self, prefix: str) -> dict[str, int]:
        """Token id that starts each verdict right after ``prefix`` (BPE merges across the quote)."""
        if prefix not in self._verdict_tokens:
            def tokens(text: str) -> list[int]:
                return [t["id"] if isinstance(t, dict) else t for t in _http_json(f"http://127.0.0.1:{self.port}/tokenize", {"content": text})["tokens"]]

            base, firsts = tokens(prefix), {}
            for verdict in IDENTITY_DECISIONS:
                full = tokens(prefix + verdict + '"')
                if full[: len(base)] != base or len(full) <= len(base):
                    raise RuntimeError(f"verdict {verdict} does not extend the prefix tokenisation")
                firsts[verdict] = full[len(base)]
            if len(set(firsts.values())) != len(firsts):
                raise RuntimeError(f"verdicts share a first token: {firsts}")
            self._verdict_tokens[prefix] = firsts
            # Token text too, for servers whose top_logprobs carry no id.
            self._verdict_pieces[prefix] = {
                verdict: _http_json(f"http://127.0.0.1:{self.port}/detokenize", {"tokens": [token]}).get("content")
                for verdict, token in firsts.items()
            }
        return self._verdict_tokens[prefix]

    def _fast_identity(self, row: dict[str, Any], content: list[dict[str, Any]]) -> tuple[str, float] | None:
        args = {"duration_s": None, "region": None, "track_id": row["focus_track_id"]}
        prefix = json.dumps({"args": args}, separators=(",", ":"))[:-1] + ',"decision":"'
        firsts = self._verdict_first_tokens(prefix)
        body = {
            "model": "local", "temperature": 0, "max_tokens": 1, "stream": False, "logprobs": True, "top_logprobs": 20,
            "messages": [{"role": "user", "content": content}, {"role": "assistant", "content": prefix}],
        }
        started = time.perf_counter()
        response = _http_json(f"http://127.0.0.1:{self.port}/v1/chat/completions", body, timeout=self.call_timeout_s)
        elapsed = time.perf_counter() - started
        try:
            entries = response["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
        except (KeyError, IndexError, TypeError):
            return None  # unexpected shape: decode the full answer instead
        pieces = self._verdict_pieces.get(prefix, {})
        scores: dict[str, float] = {}
        for verdict, token in firsts.items():
            for entry in entries:
                if entry.get("id") == token or (entry.get("id") is None and entry.get("token") == pieces.get(verdict)):
                    scores[verdict] = float(entry["logprob"])
                    break
        if not scores:
            return None
        decision = max(scores, key=scores.get)
        # Same shape as a decoded answer; logprobs feed decision_confidence as before.
        self.last_logprobs = [
            {"token": prefix, "logprob": None},
            {"token": decision, "logprob": scores[decision], "top_logprobs": [{"token": v, "logprob": p} for v, p in scores.items()]},
        ]
        answer = {"args": args, "decision": decision, "reason": "single-token identity verdict"}
        return json.dumps(answer, separators=(",", ":"), sort_keys=True), elapsed

    def __call__(self, row: dict[str, Any]) -> tuple[str, float]:
        self.last_usage = None
        images = ["data:image/jpeg;base64," + base64.b64encode(value).decode("ascii") for value in image_jpeg_bytes_list(row, self.max_side, self.crop_policy, identity_variant=self.identity_variant)]
        content = [{"type": "text", "text": prompt_for_row({**row, "_crop_policy": self.crop_policy}, identity_variant=self.identity_variant)}]
        content.extend({"type": "image_url", "image_url": {"url": value}} for value in images)
        if self.fast_identity and row.get("focus_track_id"):
            fast = self._fast_identity(row, content)
            if fast is not None:
                return fast
        body = {
            "model": "local", "temperature": 0, "max_tokens": self.max_new_tokens, "stream": False,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "onboard_decision", "strict": True, "schema": trained_key_order_schema(
                identity_decision_schema(row["focus_track_id"]) if self.identity_only and row.get("focus_track_id") else DECISION_JSON_SCHEMA
            )}},
        }
        if self.logprobs:
            body["logprobs"], body["top_logprobs"] = True, 5
        if not self.cache_prompt:
            body["cache_prompt"] = False
        started = time.perf_counter()
        response = _http_json(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            body,
            timeout=self.call_timeout_s,
        )
        elapsed = time.perf_counter() - started
        self.last_logprobs = (response["choices"][0].get("logprobs") or {}).get("content")
        self.last_usage = response.get("usage")
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
    if getattr(args, "data_root", None) is not None:
        rows = [{**row, "image_path": str((args.data_root / row["image_path"]).resolve())}
                if not Path(row["image_path"]).is_absolute() else row for row in rows]
    crop_policy = dataset_crop_policy(args.dataset, getattr(args, "crop_policy", None))
    if args.backend == "fake":
        def predictor(row):
            return json.dumps(row["assistant_target"], separators=(",", ":")), 0.001

        return evaluate_predictor(rows, predictor)
    runner = None
    try:
        if args.backend == "hf":
            runner = HFRunner(args.model, args.adapter, device=args.device, max_side=args.max_side, max_new_tokens=args.max_new_tokens, crop_policy=crop_policy, identity_variant=getattr(args, "identity_variant", None))
        else:
            if not args.gguf_model or not args.mmproj:
                raise ValueError("GGUF backend needs --gguf-model and --mmproj")
            runner = LlamaServerRunner(args.gguf_model, args.mmproj, executable=args.llama_server, adapter=args.adapter, port=args.port, context=args.context, max_new_tokens=args.max_new_tokens, device=args.device, crop_policy=crop_policy, max_side=args.max_side, identity_only=args.identity_only, fast_identity=args.fast_identity, identity_variant=getattr(args, "identity_variant", None), cache_prompt=not getattr(args, "no_prompt_cache", False))
        return evaluate_predictor(rows, runner)
    finally:
        if isinstance(runner, LlamaServerRunner):
            runner.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--data-root", type=Path, help="resolve relative dataset image paths under this repository/data root")
    parser.add_argument("--backend", choices=("hf", "gguf", "fake"), default="hf")
    parser.add_argument("--model", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--max-side", type=int, default=448)
    parser.add_argument("--crop-policy", choices=("none", "candidate_zoom_v1", "candidate_zoom_v2"))
    parser.add_argument("--identity-variant", choices=IDENTITY_VARIANTS, help="opt-in prompt/image budget experiment (otherwise use row metadata or default)")
    parser.add_argument("--no-prompt-cache", action="store_true", help="GGUF: disable per-request prompt caching for cold-prefill latency")
    parser.add_argument("--max-new-tokens", type=int, default=128)
    parser.add_argument("--gguf-model", type=Path)
    parser.add_argument("--mmproj", type=Path)
    parser.add_argument("--llama-server", type=Path, default=DEFAULT_LLAMA)
    parser.add_argument("--port", type=int, default=9933)
    parser.add_argument("--context", type=int, default=4096)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--identity-only", action="store_true", help="GGUF: rows with a focus track may only answer approach/confirm/reject")
    parser.add_argument("--fast-identity", action="store_true", help="GGUF with --identity-only: one-token verdict from log-probabilities after a prefilled answer prefix")
    args = parser.parse_args(argv)
    result = run(args)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8", newline="\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
