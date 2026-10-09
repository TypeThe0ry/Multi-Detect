"""Versioned partial-state Qwen inference adapter; frozen v2 code is unchanged.

The inherited loader verifies the pinned local base and never downloads or loads
LoRA. Generation uses the live serializer directly, without monkeypatching any
old module. Absolute cancellation/deadline enforcement remains the process
worker's responsibility. This module grants no transport or flight authority.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from .onboard_live_vlm_contract import (
    LIVE_REQUEST_CONTRACT,
    OnboardLiveVlmRequest,
    serialize_onboard_live_vlm_messages,
)
from .onboard_vlm_backend import QwenVlmBackend, QwenVlmBackendFactory
from .serialization import canonical_json, canonical_sha256, require_int, strict_json_loads


@dataclass(frozen=True, slots=True)
class LiveQwenVlmBackendFactory(QwenVlmBackendFactory):
    """Same pinned local base/budgets as v2, with an explicit live-input adapter."""

    def __call__(self):
        return LiveQwenVlmBackend(self)


class LiveVlmPreprocessingError(ValueError):
    """Rejected before generation; exact actual preprocessing records survive."""

    def __init__(self, reason: str, records: dict):
        self.preprocessing_json = canonical_json(records)
        # Direct callers can inspect this compact record (no raw pixels).
        # The frozen process worker emits only error_type, NOT these attrs or
        # exception text. A rejected native worker request therefore has no
        # host-side actual-grid receipt; this adapter does not change that IPC.
        super().__init__(reason + "; preprocessing=" + self.preprocessing_json)

    def to_mapping(self) -> dict:
        return strict_json_loads(self.preprocessing_json)


def _processor_pixel_record(processor, inputs, *, image_count: int, total_budget: int) -> dict:
    patch = require_int(
        processor.image_processor.patch_size, "processor patch_size", minimum=1, maximum=128
    )
    grid = inputs.get("image_grid_thw")
    if grid is None or len(grid.shape) != 2 or tuple(grid.shape) != (image_count, 3):
        raise ValueError("actual processor requires one image_grid_thw row per supplied image")
    rows = grid.detach().cpu().tolist()
    pixels = []
    for row in rows:
        t, h, w = (require_int(value, "actual image grid", minimum=1) for value in row)
        if t != 1:
            raise ValueError("static RGB observations cannot acquire a video temporal grid")
        pixels.append(t * h * w * patch**2)
    return {
        "actual_image_grid_thw": rows,
        "processor_patch_size": patch,
        "actual_processor_pixels_per_image": pixels,
        "actual_processor_total_pixels": sum(pixels),
        "maximum_total_processor_pixels": total_budget,
        "actual_processor_pixel_budget_verified": sum(pixels) <= total_budget,
    }


def _message_binding(messages: list[dict]) -> dict:
    records = []
    for message in messages:
        content = []
        for item in message["content"]:
            if item["type"] == "text":
                content.append({"type": "text", "text": item["text"]})
            elif item["type"] == "image":
                image = item["image"]
                content.append(
                    {
                        "type": "image",
                        "mode": image.mode,
                        "width": image.width,
                        "height": image.height,
                        "processed_rgb_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                    }
                )
            else:
                raise ValueError("unsupported live processor message content")
        records.append({"role": message["role"], "content": content})
    return {"messages": records}


class LiveQwenVlmBackend(QwenVlmBackend):
    """Reuse base loading, isolate live serialization and its audit binding."""

    def infer(self, request: OnboardLiveVlmRequest) -> dict[str, object]:
        if not isinstance(request, OnboardLiveVlmRequest):
            raise TypeError("live backend requires OnboardLiveVlmRequest")
        request.validate_binding()
        torch = self.torch
        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        messages = serialize_onboard_live_vlm_messages(request)
        message_binding = _message_binding(messages)
        image_count = len(request.model_input.frames)
        patch = require_int(
            self.processor.image_processor.patch_size,
            "processor patch_size",
            minimum=1,
            maximum=128,
        )
        merge = require_int(
            self.processor.image_processor.merge_size, "processor merge_size", minimum=1, maximum=16
        )
        # Processor smart_resize rounds to patch*merge and may upsample to its
        # default min_pixels. Set per-call bounds, never mutate the loaded base
        # processor. Still verify actual output grids; hints alone are not proof.
        image_kwargs = {
            "min_pixels": (patch * merge) ** 2,
            "max_pixels": request.max_total_pixels // image_count,
        }
        if image_kwargs["max_pixels"] < image_kwargs["min_pixels"]:
            raise LiveVlmPreprocessingError(
                "pixel budget cannot fit minimum processor grids",
                {
                    "image_count": image_count,
                    "processor_images_kwargs": image_kwargs,
                    "maximum_total_processor_pixels": request.max_total_pixels,
                    "actual_processor_pixel_budget_verified": False,
                    "actual_processor_messages_sha256": canonical_sha256(message_binding),
                },
            )
        inputs = self.processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
            images_kwargs=image_kwargs,
        )
        input_tokens = int(inputs["input_ids"].shape[-1])
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
        preprocessing_records = {
            "input_tokens": input_tokens,
            "input_tensors": input_tensor_records,
            "processor_images_kwargs": image_kwargs,
            "actual_processor_messages_sha256": canonical_sha256(message_binding),
            "execution_authority": False,
        }
        try:
            pixel_record = _processor_pixel_record(
                self.processor,
                inputs,
                image_count=image_count,
                total_budget=request.max_total_pixels,
            )
        except ValueError as exc:
            raise LiveVlmPreprocessingError(str(exc), preprocessing_records) from exc
        preprocessing_records.update(pixel_record)
        if not pixel_record["actual_processor_pixel_budget_verified"]:
            raise LiveVlmPreprocessingError(
                "actual processor grids exceed total pixel budget", preprocessing_records
            )
        if input_tokens > self.config.max_input_tokens:
            raise LiveVlmPreprocessingError(
                "processor token budget exceeded; input cannot be silently truncated",
                preprocessing_records,
            )
        inputs = inputs.to("cuda:0")
        torch.cuda.synchronize()
        preprocessed_at = time.monotonic()
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
                "backend": "native_pinned_qwen3_vl_cuda_live_partial_state_v1",
                "request_contract": LIVE_REQUEST_CONTRACT,
                "request_sha256": request.canonical_hash,
                "input_sha256": request.input_sha256,
                "factual_sensor_state_sha256": canonical_sha256(
                    {
                        "factual_sensor_state": request.model_input.to_mapping()[
                            "factual_sensor_state"
                        ]
                    }
                ),
                "actual_processor_messages_sha256": canonical_sha256(message_binding),
                "processed_images": [
                    item
                    for msg in message_binding["messages"]
                    for item in msg["content"]
                    if item["type"] == "image"
                ],
                "processed_images_role": "serializer_output_before_processor_smart_resize",
                "model_sha256": self.config.model_sha256,
                "adapter_loaded": False,
                "load_s": self.load_s,
                "input_tokens": input_tokens,
                "input_tensors": input_tensor_records,
                "processor_images_kwargs": image_kwargs,
                **pixel_record,
                "output_token_ids": output_ids,
                "generated_tokens": len(output_ids),
                "max_new_tokens_reached": len(output_ids) >= self.config.max_new_tokens,
                "preprocessing_s": preprocessed_at - started,
                "generation_s": generated_at - preprocessed_at,
                "backend_total_s": time.monotonic() - started,
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
                "cuda_free_total_bytes_after": list(torch.cuda.mem_get_info()),
                "partial_navigation_context_only": True,
                "execution_authority": False,
            },
        }


__all__ = ["LiveQwenVlmBackendFactory", "LiveQwenVlmBackend", "LiveVlmPreprocessingError"]
