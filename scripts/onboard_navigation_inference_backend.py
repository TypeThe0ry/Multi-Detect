"""Experimental prefix-only adapter for an already loaded navigation engine.

This does NOT load/verify checkpoints, own a process watchdog, or grant flight
authority. Its caller must independently admit the engine and own its lifecycle.
No default worker/backend is changed. Source/scope metadata stays outside the
frozen actor messages; supervision/gold encoding is never used for inference.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from multidetect.autonomy.onboard_navigation_inference import (
    NavigationInferenceRequest,
    parse_navigation_inference_response,
)
from multidetect.autonomy.serialization import (
    canonical_json,
    require_int,
    require_real,
    require_sha256,
)


@dataclass(frozen=True, slots=True)
class NavigationEngineIdentity:
    """Expected file identities, not proof that a supplied engine loaded them."""

    model_sha256: str
    adapter_sha256: str
    adapter_config_sha256: str

    def __post_init__(self):
        for name in ("model_sha256", "adapter_sha256", "adapter_config_sha256"):
            value = getattr(self, name)
            if require_sha256(value, name) != value:
                raise ValueError("Engine identity requires canonical lowercase SHA-256")


class NavigationBackendRejected(ValueError):
    """Raw observations survive a rejection; they are not execution evidence."""

    def __init__(self, reason, diagnostics):
        self.diagnostics_json = canonical_json(diagnostics)
        super().__init__(reason)


def _default_prefix_processor(actor, processor):
    from multidetect.autonomy import onboard_navigation_actor
    from scripts.verify_onboard_navigation_actor_inputs import process_actor

    return process_actor(actor, processor, onboard_navigation_actor)


def _default_prefix_checker(inputs, *, processor, image_count, torch_module):
    from scripts.train_onboard_vlm_navigation_pilot import tensor_record
    from scripts.verify_onboard_navigation_actor_inputs import validate_prefix_inputs

    return validate_prefix_inputs(
        inputs,
        processor=processor,
        image_count=image_count,
        torch_module=torch_module,
        tensor_record=tensor_record,
    )


class PreloadedNavigationInferenceBackend:
    """Bridge a caller-admitted engine to the new strict navigation protocol.

    Dependency injection supports bounded CPU orchestration tests. Such tests
    are not native processor/model acceptance. File hashes here are expected
    identities only: exact loading must be established by the owned loader.
    """

    def __init__(
        self,
        engine,
        identity: NavigationEngineIdentity,
        *,
        clock=time.monotonic,
        prefix_processor=_default_prefix_processor,
        prefix_checker=_default_prefix_checker,
    ):
        if type(identity) is not NavigationEngineIdentity:
            raise TypeError("identity must be NavigationEngineIdentity")
        if not all(callable(fn) for fn in (clock, prefix_processor, prefix_checker)):
            raise TypeError("clock and prefix operations must be callable")
        self.engine, self.identity = engine, identity
        self.clock, self.prefix_processor, self.prefix_checker = (
            clock,
            prefix_processor,
            prefix_checker,
        )

    def _now(self):
        return require_real(self.clock(), "host monotonic time", minimum=0)

    @staticmethod
    def _check_adapter_state(model):
        # Expected file identities do not establish the live PEFT layer state.
        if model.active_adapters != ["default"]:
            raise ValueError("Exactly the default navigation adapter must be active")
        lora_layers = [
            module
            for _name, module in model.named_modules()
            if hasattr(module, "lora_A") and hasattr(module, "lora_B")
        ]
        # Transformers' base model has a callable disable_adapters method;
        # PEFT LoRA layers expose the actual boolean state. Inspect layers.
        if not lora_layers or any(
            module.disable_adapters is not False
            or module.active_adapters != ["default"]
            or "default" not in module.lora_A
            or "default" not in module.lora_B
            for module in lora_layers
        ):
            raise ValueError("Navigation inference requires enabled default LoRA layers")

    def infer(self, request: NavigationInferenceRequest):
        if type(request) is not NavigationInferenceRequest:
            raise TypeError("navigation backend requires NavigationInferenceRequest")
        diagnostics = {
            "backend": "experimental_preloaded_navigation_engine",
            "request_sha256": request.canonical_hash,
            "generation": request.generation,
            "exact_model_load_verified_here": False,
            "sensor_authenticity_verified_here": False,
            "native_processor_or_model_acceptance_claimed": False,
            "absolute_process_watchdog_owned": False,
            "transport_authority": False,
            "execution_authority": False,
            "optimizer_performed": False,
            "generation_attempted": False,
            "generation_returned": False,
            "result_accepted": False,
        }
        try:
            request.validate_freshness(self._now())
            for name in ("model_sha256", "adapter_sha256", "adapter_config_sha256"):
                if getattr(request, name) != getattr(self.identity, name):
                    raise ValueError("Request differs from the expected preloaded engine identity")
                diagnostics[name] = getattr(self.identity, name)
            # Inspect actual adapter state, not only a declared file hash. The
            # upstream loader still owns parameter/file equality verification.
            model = self.engine.model
            self._check_adapter_state(model)
            started = self._now()
            request.validate_freshness(started)
            inputs = self.prefix_processor(request.actor_input, self.engine.processor)
            prefix = self.prefix_checker(
                inputs,
                processor=self.engine.processor,
                image_count=len(request.actor_input.frames),
                torch_module=self.engine.torch,
            )
            diagnostics["actual_prefix"] = prefix
            require_int(prefix["prefix_tokens"], "actual prefix_tokens", minimum=1, maximum=4096)
            torch = self.engine.torch
            device_inputs = {name: tensor.to("cuda:0") for name, tensor in inputs.items()}
            torch.cuda.synchronize()
            ready = self._now()
            request.validate_freshness(ready)
            # max_time is cooperative, not an absolute watchdog. Both request
            # deadline and oldest admitted RGB age are rechecked after return.
            remaining = min(
                request.deadline_s - ready,
                min(
                    frame.captured_at_s + request.max_frame_age_s - ready
                    for frame in request.source_frames
                ),
            )
            if remaining <= 0:
                raise ValueError("Preprocessing exhausted navigation time/freshness budget")
            model.eval()
            self._check_adapter_state(model)
            diagnostics["generation_attempted"] = True
            with torch.inference_mode():
                output = model.generate(
                    **device_inputs,
                    do_sample=False,
                    max_new_tokens=request.max_new_tokens,
                    max_time=remaining,
                    use_cache=True,
                )
            torch.cuda.synchronize()
            completed = self._now()
            diagnostics["generation_returned"] = True
            diagnostics["completed_at_s"] = completed
            diagnostics["inference_latency_ms"] = (completed - started) * 1000
            if (
                output.dtype != torch.int64
                or output.ndim != 2
                or output.shape[0] != 1
                or output.shape[1] <= prefix["prefix_tokens"]
            ):
                raise ValueError("Actual generation must be one prefix-plus-suffix int64 row")
            actual_ids = output[0].detach().cpu().tolist()
            prefix_ids = inputs["input_ids"][0].detach().cpu().tolist()
            if actual_ids[: prefix["prefix_tokens"]] != prefix_ids:
                raise ValueError("Actual generated sequence changed the supplied prefix")
            suffix_ids = tuple(actual_ids[prefix["prefix_tokens"] :])
            diagnostics["generated_token_ids"] = list(suffix_ids)
            diagnostics["generated_tokens"] = len(suffix_ids)
            diagnostics["max_new_tokens_reached"] = len(suffix_ids) >= request.max_new_tokens
            raw = self.engine.processor.tokenizer.decode(
                list(suffix_ids),
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )
            if type(raw) is not str:
                raise ValueError("Actual tokenizer decode must return UTF-8 text")
            diagnostics["raw_output"] = raw
            diagnostics["raw_output_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            result = parse_navigation_inference_response(
                raw,
                request,
                current_scope=request.scope,
                generation=request.generation,
                request_sha256=request.canonical_hash,
                generated_token_ids=suffix_ids,
                generated_tokens=len(suffix_ids),
                prefix_tokens=prefix["prefix_tokens"],
                completed_at_s=completed,
                inference_latency_ms=(completed - started) * 1000,
                model_sha256=self.identity.model_sha256,
                adapter_sha256=self.identity.adapter_sha256,
                adapter_config_sha256=self.identity.adapter_config_sha256,
            )
            # Token copying/decoding/validation are part of the host time budget.
            result.validate_binding(current_scope=request.scope, now_s=self._now())
            # Detect persistent state changes during generation/decoding. The
            # caller still owns exclusive lifecycle access; polling cannot prove
            # that a concurrent mutation did not change and then restore state.
            self._check_adapter_state(model)
            diagnostics["result_accepted"] = True
            return {"raw_output": raw, "navigation_result": result, "metrics": diagnostics}
        except Exception as exc:
            diagnostics["error_type"] = type(exc).__name__
            raise NavigationBackendRejected(str(exc), diagnostics) from exc


__all__ = [
    "NavigationBackendRejected",
    "NavigationEngineIdentity",
    "PreloadedNavigationInferenceBackend",
]
