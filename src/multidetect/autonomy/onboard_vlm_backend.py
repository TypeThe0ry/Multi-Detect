"""Offline pinned Qwen backend for the isolated development VLM worker.

No download, LoRA promotion, transport or flight authorization occurs here.
The same runtime request/messages contract can later be implemented by a
qualified Jetson runner. This CUDA implementation is not Jetson evidence.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from .onboard_vlm_runtime_contract import (
    OnboardVlmRuntimeRequest,
    serialize_onboard_vlm_messages,
)
from .serialization import require_int, require_sha256


@dataclass(frozen=True, slots=True)
class QwenVlmBackendFactory:
    snapshot_path: str
    model_sha256: str
    max_input_tokens: int = 8192
    max_new_tokens: int = 384

    def __post_init__(self):
        if not isinstance(self.snapshot_path, str) or not Path(self.snapshot_path).is_absolute():
            raise ValueError("snapshot_path must be an absolute local directory")
        require_sha256(self.model_sha256, "model_sha256")
        require_int(self.max_input_tokens, "max_input_tokens", minimum=256, maximum=16384)
        require_int(self.max_new_tokens, "max_new_tokens", minimum=1, maximum=1024)

    def __call__(self):
        return QwenVlmBackend(self)


class QwenVlmBackend:
    def __init__(self, config: QwenVlmBackendFactory):
        import torch
        from transformers import AutoProcessor, Qwen3VLForConditionalGeneration

        snapshot = Path(config.snapshot_path).resolve(strict=True)
        weights = snapshot / "model.safetensors"
        hasher = hashlib.sha256()
        with weights.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                hasher.update(block)
        if hasher.hexdigest() != config.model_sha256:
            raise ValueError("local model weights differ from deployment identity")
        if not torch.cuda.is_available():
            raise RuntimeError("native Qwen worker requires CUDA")
        self.torch, self.config = torch, config
        started = time.monotonic()
        self.processor = AutoProcessor.from_pretrained(
            str(snapshot), local_files_only=True, trust_remote_code=False
        )
        self.model = Qwen3VLForConditionalGeneration.from_pretrained(
            str(snapshot),
            local_files_only=True,
            trust_remote_code=False,
            torch_dtype=torch.bfloat16,
            attn_implementation="sdpa",
            device_map={"": "cuda:0"},
        ).eval()
        torch.cuda.synchronize()
        self.load_s = time.monotonic() - started

    def infer(self, request: OnboardVlmRuntimeRequest) -> dict[str, object]:
        torch = self.torch
        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        messages = serialize_onboard_vlm_messages(request)
        inputs = self.processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        )
        input_tokens = int(inputs["input_ids"].shape[-1])
        if input_tokens > self.config.max_input_tokens:
            raise ValueError("processor token budget exceeded; input cannot be silently truncated")
        input_tensor_records = {
            name: {
                "shape": list(tensor.shape),
                "dtype": str(tensor.dtype),
                "sha256": hashlib.sha256(
                    tensor.detach().contiguous().cpu().view(torch.uint8).numpy().tobytes()
                ).hexdigest(),
            }
            for name, tensor in inputs.items()
            if isinstance(tensor, torch.Tensor)
        }
        inputs = inputs.to("cuda:0")
        torch.cuda.synchronize()
        preprocessed_at = time.monotonic()
        # Host watchdog enforces the absolute budget including queueing,
        # preprocessing and result validation. max_time cannot provide that.
        remaining = (
            request.model_input.deadline_at_s
            - request.model_input.requested_at_s
            - (preprocessed_at - started)
        )
        if remaining <= 0:
            raise TimeoutError("preprocessing exhausted deadline")
        with torch.inference_mode():
            generated = self.model.generate(
                **inputs,
                max_new_tokens=self.config.max_new_tokens,
                do_sample=False,
                max_time=remaining,
                use_cache=True,
            )
        torch.cuda.synchronize()
        generated_at = time.monotonic()
        output_ids = generated[0, input_tokens:].detach().cpu().tolist()
        raw = self.processor.tokenizer.decode(
            output_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False
        )
        return {
            "raw_output": raw,
            "metrics": {
                "backend": "native_pinned_qwen3_vl_cuda",
                "model_sha256": self.config.model_sha256,
                "adapter_loaded": False,
                "load_s": self.load_s,
                "input_tokens": input_tokens,
                "input_tensors": input_tensor_records,
                "output_token_ids": output_ids,
                "generated_tokens": len(output_ids),
                "max_new_tokens_reached": len(output_ids) >= self.config.max_new_tokens,
                "preprocessing_s": preprocessed_at - started,
                "generation_s": generated_at - preprocessed_at,
                "backend_total_s": time.monotonic() - started,
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
                "cuda_free_total_bytes_after": list(torch.cuda.mem_get_info()),
                "execution_authority": False,
            },
        }


__all__ = ["QwenVlmBackendFactory", "QwenVlmBackend"]
