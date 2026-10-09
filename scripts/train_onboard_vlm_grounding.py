"""Development-only factual Qwen3-VL LoRA: no flight policy or transport.

The independent dataset reviewer owns source/label/split admission. Only the
assistant's small factual JSON reply is supervised. Heavy dependencies are lazy.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import inspect
import io
import json
import math
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scripts import probe_onboard_vlm as probe

MODEL_ID = "Qwen/Qwen3-VL-2B-Instruct"
REVISION = "89644892e4d85e24eaac8bacfd4f463576704203"
CONTRACT = "multidetect.onboard-vlm-grounding-lora-development-v1"
PLAN_SHA256 = "142c93dfbc89d68a01b75661ae91fdf7f1a5d7b48cd4077b3cb1b6175588eeff"
TARGET_FIELDS = frozenset(
    {"target_reference_id", "current_target_visibility", "gps", "ned", "aircraft_motion"}
)
LANGUAGE_TARGET = re.compile(r"model\.language_model\.layers\.\d+\.self_attn\.(q_proj|v_proj)")
SYSTEM_PROMPT = """You are an offline development visual fact-grounding model, not a pilot.
The reference image shows which entity to examine in the current RGB image.
Return only one JSON object with exactly these fields:
{"target_reference_id":"copy the supplied opaque ID",
"current_target_visibility":"visible|not_confirmed",
"gps":"unknown","ned":"unknown","aircraft_motion":"unknown"}.
Choose visible only when the referenced entity is visually supported in the
CURRENT image. A remembered/reference entity is not proof of current visibility.
Otherwise choose not_confirmed. No telemetry or positioning evidence was supplied:
motion, GPS and NED remain unknown. Do not output flight actions, coordinates,
distances, class names, commentary or additional fields.
"""


@dataclass(frozen=True)
class TrainingBudget:
    epochs: int = 1
    max_optimizer_steps: int = 200
    max_pixels_per_image: int = 200704
    max_input_tokens: int = 4096
    max_new_tokens: int = 192
    rank: int = 4
    alpha: int = 8
    learning_rate: float = 0.0001
    seed: int = 20261007
    gradient_checkpointing: bool = True

    def validate(self) -> None:
        for name, lower, upper in (
            ("epochs", 1, 5),
            ("max_optimizer_steps", 1, 200),
            ("max_pixels_per_image", 4096, 262144),
            ("max_input_tokens", 128, 4096),
            ("max_new_tokens", 32, 256),
            ("rank", 1, 8),
            ("alpha", 1, 32),
            ("seed", 0, 2**32 - 1),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name} exceeds the declared development budget")
        if (
            type(self.learning_rate) not in {int, float}
            or not math.isfinite(self.learning_rate)
            or not 0 < self.learning_rate <= 0.001
        ):
            raise ValueError("learning_rate exceeds the bounded LoRA budget")
        if type(self.gradient_checkpointing) is not bool:
            raise ValueError("gradient_checkpointing must be boolean")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def strict_factual_json(payload: bytes) -> object:
    parsed = probe.strict_json(payload)

    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Factual reply contains a non-finite numeric value")
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        elif isinstance(value, list):
            for item in value:
                finite(item)

    finite(parsed)
    return parsed


def load_training_plan(path: Path, expected_sha256: str) -> dict:
    probe.require_sha256(expected_sha256)
    payload = path.read_bytes()
    if expected_sha256 != PLAN_SHA256 or hashlib.sha256(payload).hexdigest() != PLAN_SHA256:
        raise ValueError("Unknown or changed preregistered experiment plan")
    document = probe.strict_json(payload)
    if document["model_id"] != MODEL_ID or document["model_revision"] != REVISION:
        raise ValueError("Plan model identity differs from the frozen experiment")
    return document


def budget_from_plan(plan: dict) -> TrainingBudget:
    optimization = plan["optimization"]
    budget = TrainingBudget(
        epochs=optimization["epochs"],
        max_optimizer_steps=optimization["maximum_optimizer_steps"],
        max_pixels_per_image=optimization["maximum_image_pixels"],
        max_input_tokens=optimization["maximum_input_tokens"],
        max_new_tokens=optimization["maximum_generated_tokens"],
        rank=optimization["rank"],
        alpha=optimization["alpha"],
        learning_rate=optimization["learning_rate"],
        seed=optimization["seed"],
        gradient_checkpointing=optimization["gradient_checkpointing"],
    )
    budget.validate()
    return budget


def response_only_labels(
    full_ids: list[int], prefix_ids: list[int], attention_mask: list[int], image_token_ids: set[int]
) -> list[int]:
    """Exact processor-token boundary, never estimated character/token lengths."""
    if not prefix_ids or len(full_ids) <= len(prefix_ids):
        raise ValueError("Assistant response must have a nonempty exact prefix and suffix")
    if full_ids[: len(prefix_ids)] != prefix_ids:
        raise ValueError("Processor full chat does not preserve the exact generation prefix")
    if len(attention_mask) != len(full_ids) or any(value not in {0, 1} for value in attention_mask):
        raise ValueError("Malformed attention/padding mask")
    labels = [
        token if index >= len(prefix_ids) and mask == 1 and token not in image_token_ids else -100
        for index, (token, mask) in enumerate(zip(full_ids, attention_mask, strict=True))
    ]
    if not any(label != -100 for label in labels):
        raise ValueError("No actual assistant tokens remain after response-only masking")
    return labels


def validate_target(target: dict, target_reference_id: str) -> None:
    if not isinstance(target, dict) or set(target) != TARGET_FIELDS:
        raise ValueError("Factual target fields differ from the fixed response contract")
    if target["target_reference_id"] != target_reference_id:
        raise ValueError("Factual response names another target reference")
    if not isinstance(target["current_target_visibility"], str) or (
        target["current_target_visibility"] not in {"visible", "not_confirmed"}
    ):
        raise ValueError("Unsupported current visibility label")
    if any(target[field] != "unknown" for field in ("gps", "ned", "aircraft_motion")):
        raise ValueError("Unknown navigation/motion facts cannot become expert labels")


def admit_samples(document: dict, budget: TrainingBudget) -> dict[str, list[dict]]:
    """Additional trainer-specific admission after the independent source validator."""
    budget.validate()
    result = {split: [] for split in ("train", "val", "test")}
    seen = set()
    for sample in document["samples"]:
        identifier = sample["sample_id"]
        if not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
            raise ValueError("Sample IDs must be opaque 32-hex identities")
        if identifier in seen:
            raise ValueError("Duplicate sample ID")
        seen.add(identifier)
        split = sample["split"]
        if split not in result:
            raise ValueError("Unsupported dataset split")
        model_input = sample["model_input"]
        if set(model_input) != {"prompt", "target_reference_id", "images", "context"}:
            raise ValueError(
                "Only declared RGB/prompt/reference/unknown context can enter the model"
            )
        if not isinstance(model_input["prompt"], str) or not model_input["prompt"].strip():
            raise ValueError("Missing original prompt")
        reference_id = model_input["target_reference_id"]
        if not isinstance(reference_id, str) or re.fullmatch(r"[0-9a-f]{32}", reference_id) is None:
            raise ValueError("Target reference must use an opaque 32-hex identity")
        images = model_input["images"]
        if len(images) != 2 or [image["role"] for image in images] != ["reference", "current"]:
            raise ValueError(
                "Exactly two RGB images reference/current are required; no silent drop"
            )
        context = model_input["context"]
        if context != {"gps": "unknown", "ned": "unknown", "aircraft_motion": "unknown"}:
            raise ValueError("Navigation context must remain explicitly unknown")
        validate_target(sample["assistant_target"], reference_id)
        result[split].append(sample)
    if any(not samples for samples in result.values()):
        raise ValueError("Independent nonempty train/val/test splits are required")
    if len(result["train"]) * budget.epochs > budget.max_optimizer_steps:
        raise ValueError("The complete declared epochs exceed the optimizer ceiling; no truncation")
    return result


def image_messages(
    sample: dict, dataset_directory: Path, budget: TrainingBudget
) -> tuple[list, list]:
    from PIL import Image

    model_input = sample["model_input"]
    content = []
    preprocessing = []
    for image_spec in model_input["images"]:
        path = (dataset_directory / image_spec["path"]).resolve(strict=True)
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != image_spec["sha256"]:
            raise ValueError("Bound RGB changed while preparing the model input")
        with Image.open(io.BytesIO(payload)) as source:
            image = source.convert("RGB")
        if image.size != (image_spec["width"], image_spec["height"]):
            raise ValueError("Actual RGB dimensions differ from the admitted input")
        scale = min(1.0, math.sqrt(budget.max_pixels_per_image / (image.width * image.height)))
        if scale < 1:
            image = image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
        content.append({"type": "text", "text": f"Image role: {image_spec['role']}"})
        content.append({"type": "image", "image": image})
        preprocessing.append(
            {
                "role": image_spec["role"],
                "source_sha256": image_spec["sha256"],
                "processed_width": image.width,
                "processed_height": image.height,
                "processed_rgb_pixels_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
            }
        )
    content.append({"type": "text", "text": model_input["prompt"]})
    content.append(
        {
            "type": "text",
            "text": canonical_bytes(
                {
                    "target_reference_id": model_input["target_reference_id"],
                    "context": model_input["context"],
                }
            ).decode("utf-8"),
        }
    )
    return [
        {"role": "system", "content": [{"type": "text", "text": SYSTEM_PROMPT}]},
        {"role": "user", "content": content},
    ], preprocessing


def audit_trainable_parameters(model) -> dict:
    trainable = []
    frozen_count = 0
    for name, parameter in model.named_parameters():
        if not parameter.requires_grad:
            frozen_count += parameter.numel()
            continue
        if (
            ".lora_A." not in name
            and ".lora_B." not in name
            or ".model.language_model.layers." not in name
            or not any(f".self_attn.{projection}." in name for projection in ("q_proj", "v_proj"))
            or ".visual." in name
        ):
            raise ValueError(f"A non-language q/v LoRA parameter is trainable: {name}")
        trainable.append({"name": name, "numel": parameter.numel(), "dtype": str(parameter.dtype)})
    if not trainable:
        raise ValueError("No admitted language-only LoRA parameters are trainable")
    return {
        "trainable_parameters": trainable,
        "trainable_parameter_count": sum(item["numel"] for item in trainable),
        "frozen_parameter_count": frozen_count,
        "all_base_and_vision_parameters_frozen": True,
        "trainable_dtypes": sorted({item["dtype"] for item in trainable}),
        "frozen_base_dtypes": sorted(
            {str(p.dtype) for p in model.parameters() if not p.requires_grad}
        ),
    }


def parameter_digest(model, *, trainable: bool) -> str:
    """Hash actual in-memory tensor bytes, not merely the original checkpoint."""
    import torch

    digest = hashlib.sha256()
    for name, parameter in model.named_parameters():
        is_lora = ".lora_A." in name or ".lora_B." in name
        if (trainable and not parameter.requires_grad) or (not trainable and is_lora):
            continue
        digest.update(canonical_bytes([name, list(parameter.shape), str(parameter.dtype)]))
        tensor = parameter.detach().cpu().contiguous().view(torch.uint8)
        digest.update(tensor.numpy().tobytes())
        del tensor
    return digest.hexdigest()


def causal_response_loss(logits, full_labels):
    import torch

    if logits.ndim != 3 or full_labels.ndim != 2 or logits.shape[0] != full_labels.shape[0]:
        raise ValueError("Causal logits/labels have incompatible batch shapes")
    if not 2 <= logits.shape[1] <= full_labels.shape[1]:
        raise ValueError("The causal response window must include the previous prefix token")
    shifted_labels = full_labels[:, -logits.shape[1] :][:, 1:]
    shifted_logits = logits[:, :-1, :].float()
    return torch.nn.functional.cross_entropy(
        shifted_logits.reshape(-1, shifted_logits.shape[-1]),
        shifted_labels.reshape(-1),
        ignore_index=-100,
    )


class GroundingBackend:
    def __init__(self, snapshot: Path, budget: TrainingBudget):
        import peft
        import torch
        import transformers

        if peft.__version__ != "0.17.1":
            raise RuntimeError("This declared development experiment requires PEFT 0.17.1")
        if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
            raise RuntimeError("A real BF16-compatible CUDA GPU is required")
        self.torch, self.peft, self.transformers = torch, peft, transformers
        self.budget = budget
        torch.manual_seed(budget.seed)
        torch.cuda.manual_seed_all(budget.seed)
        started = time.perf_counter()
        self.processor = transformers.AutoProcessor.from_pretrained(
            str(snapshot),
            local_files_only=True,
            trust_remote_code=False,
            max_pixels=budget.max_pixels_per_image,
        )
        base = transformers.Qwen3VLForConditionalGeneration.from_pretrained(
            str(snapshot),
            local_files_only=True,
            trust_remote_code=False,
            torch_dtype=torch.bfloat16,
            attn_implementation="sdpa",
            device_map={"": "cuda:0"},
        )
        base.requires_grad_(False)
        targets = [
            name for name, _module in base.named_modules() if LANGUAGE_TARGET.fullmatch(name)
        ]
        if not targets:
            raise ValueError("Actual model does not expose the admitted language q/v projections")
        config = peft.LoraConfig(
            task_type="CAUSAL_LM",
            r=budget.rank,
            lora_alpha=budget.alpha,
            lora_dropout=0.0,
            target_modules=targets,
            bias="none",
            init_lora_weights=True,
        )
        self.model = peft.get_peft_model(base, config)
        self.model.peft_config["default"].base_model_name_or_path = MODEL_ID
        self.model.peft_config["default"].revision = REVISION
        self.model.config.use_cache = False
        if budget.gradient_checkpointing:
            self.model.gradient_checkpointing_enable(
                gradient_checkpointing_kwargs={"use_reentrant": False}
            )
            self.model.enable_input_require_grads()
        self.trainable_audit = audit_trainable_parameters(self.model)
        self.base_digest_initial = parameter_digest(self.model, trainable=False)
        self.adapter_digest_initial = parameter_digest(self.model, trainable=True)
        torch.cuda.synchronize()
        self.load_s = time.perf_counter() - started

    def runtime_records(self) -> list[dict]:
        paths = {Path(__file__), Path(sys.executable)}
        for obj in (self.model.__class__, self.processor.__class__):
            paths.add(Path(inspect.getfile(obj)))
        for name, module in tuple(sys.modules.items()):
            if name in {
                "torch",
                "torch._C",
                "transformers",
                "peft",
                "safetensors",
                "tokenizers",
            } or (name.startswith(("peft.", "transformers.models.qwen3_vl", "transformers.loss."))):
                filename = getattr(module, "__file__", None)
                if filename and Path(filename).is_file():
                    paths.add(Path(filename))
        return [probe.file_record(path) for path in sorted(paths, key=str)]

    def encode(self, sample: dict, dataset_directory: Path) -> dict:
        messages, preprocessing = image_messages(sample, dataset_directory, self.budget)
        common = {"tokenize": True, "return_dict": True, "return_tensors": "pt"}
        prefix = self.processor.apply_chat_template(messages, add_generation_prompt=True, **common)
        target_text = canonical_bytes(sample["assistant_target"]).decode("utf-8")
        full = self.processor.apply_chat_template(
            messages + [{"role": "assistant", "content": [{"type": "text", "text": target_text}]}],
            add_generation_prompt=False,
            **common,
        )
        full_ids = full["input_ids"][0].tolist()
        prefix_ids = prefix["input_ids"][0].tolist()
        if len(full_ids) > self.budget.max_input_tokens:
            raise ValueError("Actual full token sequence exceeds the declared cap; no truncation")
        grids = full["image_grid_thw"].tolist()
        patch_size = self.processor.image_processor.patch_size
        if len(grids) != 2 or any(
            t != 1 or h * w * patch_size**2 > self.budget.max_pixels_per_image for t, h, w in grids
        ):
            raise ValueError("Actual processor image grids exceed the two-image pixel budget")
        if not self.torch.equal(full["image_grid_thw"], prefix["image_grid_thw"]) or (
            not self.torch.equal(full["pixel_values"], prefix["pixel_values"])
        ):
            raise ValueError("Prefix and full processor image tensors differ")
        image_tokens = {
            self.processor.tokenizer.convert_tokens_to_ids(value)
            for value in ("<|image_pad|>", "<|video_pad|>", "<|vision_start|>", "<|vision_end|>")
        }
        labels = response_only_labels(
            full_ids, prefix_ids, full["attention_mask"][0].tolist(), image_tokens
        )
        return {
            "prefix": prefix,
            "full": full,
            "labels": self.torch.tensor([labels], dtype=self.torch.long),
            "prefix_tokens": len(prefix_ids),
            "total_tokens": len(full_ids),
            "response_tokens": len(full_ids) - len(prefix_ids),
            "supervised_tokens": sum(label != -100 for label in labels),
            "preprocessing": preprocessing,
            "actual_image_grid_thw": grids,
            "actual_image_pixels": [h * w * patch_size**2 for _t, h, w in grids],
            "input_tensors": {
                name: {
                    "shape": list(tensor.shape),
                    "dtype": str(tensor.dtype),
                    "sha256": hashlib.sha256(
                        tensor.detach().cpu().contiguous().view(self.torch.uint8).numpy().tobytes()
                    ).hexdigest(),
                }
                for name, tensor in full.items()
            },
            "response_labels_sha256": hashlib.sha256(canonical_bytes(labels)).hexdigest(),
        }

    def loss(self, encoded: dict):
        self.model.train()
        self.model.base_model.model.visual.eval()
        full = {name: tensor.to("cuda:0") for name, tensor in encoded["full"].items()}
        labels = encoded["labels"].to("cuda:0")
        # Only response-adjacent logits are needed. This is exactly ordinary
        # causal response-only loss, without allocating logits for the prefix.
        window = encoded["response_tokens"] + 1
        output = self.model(**full, use_cache=False, logits_to_keep=window)
        return causal_response_loss(output.logits, labels)

    def backward_preflight(self, encoded: dict) -> dict:
        torch = self.torch
        torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        self.model.zero_grad(set_to_none=True)
        loss = self.loss(encoded)
        if not torch.isfinite(loss).item():
            raise ValueError("Actual preflight loss is not finite")
        loss.backward()
        torch.cuda.synchronize()
        gradient_sum = 0.0
        for name, parameter in self.model.named_parameters():
            if not parameter.requires_grad and parameter.grad is not None:
                raise ValueError(f"Frozen base parameter unexpectedly has a gradient: {name}")
            if parameter.requires_grad and parameter.grad is not None:
                if not torch.isfinite(parameter.grad).all().item():
                    raise ValueError("LoRA gradient is not finite")
                gradient_sum += parameter.grad.detach().float().abs().sum().item()
        if gradient_sum <= 0:
            raise ValueError("No nonzero actual LoRA gradient reached the adapter")
        result = {
            "actual_forward_backward_performed": True,
            "optimizer_step_performed": False,
            "loss": loss.detach().item(),
            "gradient_absolute_sum": gradient_sum,
            "elapsed_s": time.perf_counter() - started,
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        }
        self.model.zero_grad(set_to_none=True)
        return result

    def generate(self, encoded: dict, *, parent: bool) -> dict:
        torch = self.torch
        self.model.eval()
        prefix = {name: tensor.to("cuda:0") for name, tensor in encoded["prefix"].items()}
        context = self.model.disable_adapter() if parent else contextlib.nullcontext()
        torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        with context, torch.inference_mode():
            output = self.model.generate(
                **prefix,
                do_sample=False,
                max_new_tokens=self.budget.max_new_tokens,
                max_time=30.0,
                use_cache=True,
            )
        torch.cuda.synchronize()
        ids = output[0, prefix["input_ids"].shape[-1] :].tolist()
        return {
            "raw_output": self.processor.tokenizer.decode(
                ids, skip_special_tokens=True, clean_up_tokenization_spaces=False
            ),
            "output_token_ids": ids,
            "generated_tokens": len(ids),
            "token_cap_reached": len(ids) >= self.budget.max_new_tokens,
            "elapsed_s": time.perf_counter() - started,
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        }

    def verify_saved_adapter(self, path: Path, encoded: dict, candidate: dict) -> dict:
        def state_digest(adapter_name):
            state = self.peft.get_peft_model_state_dict(self.model, adapter_name=adapter_name)
            digest = hashlib.sha256()
            for name, tensor in sorted(state.items()):
                digest.update(canonical_bytes([name, list(tensor.shape), str(tensor.dtype)]))
                digest.update(
                    tensor.detach().cpu().contiguous().view(self.torch.uint8).numpy().tobytes()
                )
            return digest.hexdigest()

        initial = state_digest("default")
        self.model.load_adapter(str(path), adapter_name="sealed_reload", is_trainable=False)
        self.model.set_adapter("sealed_reload")
        loaded = state_digest("sealed_reload")
        if initial != loaded:
            raise ValueError(
                "Actual offline loaded adapter tensor bytes differ from trained adapter"
            )
        generation = self.generate(encoded, parent=False)
        if generation["output_token_ids"] != candidate["output_token_ids"]:
            raise ValueError("Reloaded adapter changed the declared representative generation")
        return {
            "offline_reload_performed": True,
            "explicit_existing_pinned_base_used": True,
            "original_adapter_state_sha256": initial,
            "loaded_adapter_state_sha256": loaded,
            "generation_token_ids_equal": True,
            "generation": generation,
        }


def evaluate_rows(
    backend,
    samples: list[dict],
    encoded: dict,
    *,
    parent: bool,
    artifact_directory: Path | None = None,
) -> dict:
    results = []
    if artifact_directory is not None:
        artifact_directory.mkdir(exist_ok=False)
    for completed, sample in enumerate(samples, 1):
        try:
            generated = backend.generate(encoded[sample["sample_id"]], parent=parent)
        except Exception as exc:
            if artifact_directory is not None:
                _write_json(
                    artifact_directory / f"{sample['sample_id']}.json",
                    {
                        "sample_id": sample["sample_id"],
                        "split": sample["split"],
                        "native_generation_completed": False,
                        "failure_type": type(exc).__name__,
                        "failure": str(exc),
                    },
                )
            raise
        accepted = False
        exact = False
        parsed = None
        failure = None
        try:
            if generated["token_cap_reached"] or generated["elapsed_s"] > 30.0:
                raise ValueError(
                    "Generation reached its token cap or completed after the soft budget"
                )
            parsed = strict_factual_json(generated["raw_output"].encode("utf-8"))
            validate_target(parsed, sample["model_input"]["target_reference_id"])
            accepted = True
            exact = parsed == sample["assistant_target"]
        except (ValueError, TypeError, KeyError) as exc:
            failure = str(exc)
        results.append(
            {
                "sample_id": sample["sample_id"],
                "split": sample["split"],
                "model_input_sha256": sample["model_input_sha256"],
                "expected_target": sample["assistant_target"],
                "native_generation_completed": True,
                "source_clip_id": sample["provenance"]["source_clip_id"],
                "reference_rgb_sha256": sample["model_input"]["images"][0]["sha256"],
                "current_rgb_sha256": sample["model_input"]["images"][1]["sha256"],
                "prompt_sha256": hashlib.sha256(
                    sample["model_input"]["prompt"].encode("utf-8")
                ).hexdigest(),
                **generated,
                "parsed_output": parsed,
                "strict_fact_reply_accepted": accepted,
                "exact_factual_match": exact,
                "failure": failure,
            }
        )
        if artifact_directory is not None:
            _write_json(artifact_directory / f"{sample['sample_id']}.json", results[-1])
        print(
            json.dumps(
                {
                    "phase": "parent" if parent else "candidate",
                    "sample_id": sample["sample_id"],
                    "split": sample["split"],
                    "completed": completed,
                    "total": len(samples),
                    "accepted": accepted,
                    "exact_factual_match": exact,
                    "visible_gold": sample["assistant_target"]["current_target_visibility"],
                    "prediction": parsed.get("current_target_visibility")
                    if isinstance(parsed, dict)
                    else None,
                    "elapsed_s": generated["elapsed_s"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
    return {
        "adapter_disabled": parent,
        "checkpoint_selection_performed": False,
        "samples": results,
        "count": len(results),
        "strict_reply_count": sum(item["strict_fact_reply_accepted"] for item in results),
        "exact_match_count": sum(item["exact_factual_match"] for item in results),
        "metrics": grounding_metrics(results),
    }


def grounding_metrics(results: list[dict]) -> dict:
    def confusion(rows):
        counts = {name: 0 for name in ("tp", "tn", "fp", "fn", "invalid")}
        for row in rows:
            expected_visible = row["expected_target"]["current_target_visibility"] == "visible"
            parsed = row["parsed_output"]
            prediction = (
                parsed.get("current_target_visibility")
                if (row["strict_fact_reply_accepted"] and isinstance(parsed, dict))
                else None
            )
            if not isinstance(prediction, str) or prediction not in {"visible", "not_confirmed"}:
                counts["invalid"] += 1
                counts["fn" if expected_visible else "fp"] += 1
            elif prediction == "visible":
                counts["tp" if expected_visible else "fp"] += 1
            else:
                counts["fn" if expected_visible else "tn"] += 1
        sensitivity = (
            counts["tp"] / (counts["tp"] + counts["fn"]) if (counts["tp"] + counts["fn"]) else None
        )
        specificity = (
            counts["tn"] / (counts["tn"] + counts["fp"]) if (counts["tn"] + counts["fp"]) else None
        )
        return {
            **counts,
            "sensitivity": sensitivity,
            "specificity": specificity,
            "balanced_accuracy": (sensitivity + specificity) / 2
            if sensitivity is not None and specificity is not None
            else None,
        }

    groups = {}
    for row in results:
        key = (row["split"], row["source_clip_id"])
        groups.setdefault(key, []).append(row)
    swaps = []
    for rows in groups.values():
        for positive in rows:
            if positive["expected_target"]["current_target_visibility"] != "visible":
                continue
            for negative in rows:
                if negative["expected_target"]["current_target_visibility"] != "not_confirmed":
                    continue
                if (
                    any(
                        positive[key] != negative[key]
                        for key in (
                            "reference_rgb_sha256",
                            "prompt_sha256",
                        )
                    )
                    or positive["expected_target"]["target_reference_id"]
                    != (negative["expected_target"]["target_reference_id"])
                ):
                    continue
                pos = positive["parsed_output"]
                neg = negative["parsed_output"]
                consistent = (
                    positive["strict_fact_reply_accepted"]
                    and negative["strict_fact_reply_accepted"]
                    and pos["current_target_visibility"] == "visible"
                    and neg["current_target_visibility"] == "not_confirmed"
                )
                swaps.append(
                    {
                        "split": positive["split"],
                        "clip_id": positive["source_clip_id"],
                        "positive_sample_id": positive["sample_id"],
                        "zero_pixel_sample_id": negative["sample_id"],
                        "prediction_flip_consistent": consistent,
                    }
                )
    unknown_count = sum(
        isinstance(row["parsed_output"], dict)
        and all(
            row["parsed_output"].get(field) == "unknown"
            for field in ("gps", "ned", "aircraft_motion")
        )
        for row in results
    )
    return {
        "visibility_confusion": confusion(results),
        "visibility_metrics_require_strict_valid_reply": True,
        "per_split_visibility": {
            split: confusion([row for row in results if row["split"] == split])
            for split in sorted({row["split"] for row in results})
        },
        "per_clip_visibility": [
            {"split": split, "clip_id": clip, **confusion(rows)}
            for (split, clip), rows in sorted(groups.items())
        ],
        "unknown_navigation_fact_count": unknown_count,
        "unknown_navigation_fact_accuracy": unknown_count / len(results) if results else None,
        "unknown_facts_included_in_visibility_accuracy": False,
        "same_reference_prompt_current_rgb_swap_pairs": swaps,
        "swap_pair_count": len(swaps),
        "consistent_swap_pair_count": sum(row["prediction_flip_consistent"] for row in swaps),
        "swap_pairs_are_independent_observations": False,
    }


def _write_json(path: Path, value: object) -> None:
    payload = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(payload)


def run(args, *, backend_factory=GroundingBackend, dataset_validator=None) -> int:
    if args.mode not in {"preflight", "train"}:
        raise ValueError("Unknown experiment mode")
    budget = TrainingBudget(
        epochs=args.epochs,
        max_optimizer_steps=args.max_optimizer_steps,
        max_pixels_per_image=args.max_pixels_per_image,
        max_input_tokens=args.max_input_tokens,
        max_new_tokens=args.max_new_tokens,
        rank=args.rank,
        alpha=args.alpha,
        learning_rate=args.learning_rate,
        seed=args.seed,
        gradient_checkpointing=not args.no_gradient_checkpointing,
    )
    budget.validate()
    plan = load_training_plan(args.plan, args.plan_sha256)
    if budget != budget_from_plan(plan):
        raise ValueError("Invocation hyperparameters differ from the preregistered fixed plan")
    if args.model_id != MODEL_ID or args.revision != REVISION:
        raise ValueError("Only the declared official Qwen3-VL-2B revision is admitted")
    probe.require_sha256(args.dataset_sha256)
    if args.review_receipt is None:
        raise ValueError("An independent dataset review receipt is required even for preflight")
    cache = probe.require_d_storage(args.cache_directory)
    output = probe.require_d_storage(args.output_directory)
    if output.exists():
        raise FileExistsError("The trainer never overwrites prior results")
    source_paths = [
        Path(__file__),
        Path(probe.__file__),
        args.dataset_manifest,
        args.review_receipt,
    ]
    source_paths.append(Path(__file__).parent / "build_onboard_vlm_grounding_dataset.py")
    source_paths.append(args.plan)
    source_root = Path(__file__).resolve().parents[1]
    source_paths.append(source_root / plan["source_manifest"])
    sources = [probe.file_record(path) for path in source_paths]
    if sources[2]["sha256"] != args.dataset_sha256:
        raise ValueError("Dataset bytes differ from the declared invocation SHA")
    if sources[-1]["sha256"] != plan["source_manifest_sha256"]:
        raise ValueError("Preregistered 3D source manifest changed")
    if sources[-2]["sha256"] != args.plan_sha256:
        raise ValueError("Plan changed between parsing and source snapshot")
    if dataset_validator is None:
        from scripts.build_onboard_vlm_grounding_dataset import validate_grounding_dataset

        dataset_validator = validate_grounding_dataset
    document = dataset_validator(
        args.dataset_manifest,
        args.dataset_sha256,
        review_receipt=args.review_receipt,
        require_review=True,
    )
    splits = admit_samples(document, budget)
    for sample in document["samples"]:
        for image in sample["model_input"]["images"]:
            record = probe.file_record(args.dataset_manifest.parent / image["path"])
            if record["sha256"] != image["sha256"]:
                raise ValueError("Dataset RGB changed after independent admission")
            sources.append(record)
    probe.verify_records(sources)
    probe.configure_d_cache(cache)
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "mode": args.mode,
        "model_id": MODEL_ID,
        "revision": REVISION,
        "dataset_sha256": args.dataset_sha256,
        "review_receipt_sha256": sources[3]["sha256"],
        "training_plan_sha256": args.plan_sha256,
        "budget": asdict(budget),
        "batch_size": 1,
        "source_files": sources,
        "development_training_authorized": True,
        "response_only_loss": True,
        "base_vision_frozen": False,
        "navigation_context_unknown": True,
        "flight_action_training_performed": False,
        "vlm_training_performed": False,
        "optimizer_steps_completed": 0,
        "candidate_promoted": False,
        "final_release_allowed": False,
        "transport_created": False,
        "execution_authority": False,
        "hil_real_evidence": False,
        "physical_performance_claimed": False,
        "outdoor_gnss_evidence": False,
        "checkpoint_selection_performed": False,
        "hard_deadline_enforced": False,
        "cancellable_inference_worker": False,
        "native_exit_code": 1,
        "declared_split_sample_ids": {
            split: [row["sample_id"] for row in rows] for split, rows in splits.items()
        },
    }
    _write_json(output / "invocation.json", report)
    invocation_record = probe.file_record(output / "invocation.json")
    try:
        snapshot, provenance = probe.prepare_model_snapshot(MODEL_ID, REVISION, cache)
        if not any(
            record["sha256"] == plan["base_weights_sha256"]
            and Path(record["path"]).name == "model.safetensors"
            for record in provenance["files"]
        ):
            raise ValueError("Actual base weights differ from the preregistered pinned checkpoint")
        report["model_provenance"] = provenance
        backend = backend_factory(snapshot, budget)
        report["runtime_files"] = backend.runtime_records()
        report["runtime_versions"] = {
            "torch": backend.torch.__version__,
            "transformers": backend.transformers.__version__,
            "peft": backend.peft.__version__,
            "cuda": backend.torch.version.cuda,
            "gpu": backend.torch.cuda.get_device_name(0),
        }
        report["model_load_s"] = backend.load_s
        report["trainable_audit"] = backend.trainable_audit
        report["base_vision_frozen"] = True
        report["base_parameter_sha256_before"] = backend.base_digest_initial
        report["adapter_parameter_sha256_before"] = backend.adapter_digest_initial
        encoded = {
            sample["sample_id"]: backend.encode(sample, args.dataset_manifest.parent)
            for sample in document["samples"]
        }
        report["encoded_samples"] = {
            identifier: {
                name: value[name]
                for name in (
                    "prefix_tokens",
                    "total_tokens",
                    "response_tokens",
                    "supervised_tokens",
                    "preprocessing",
                    "actual_image_grid_thw",
                    "actual_image_pixels",
                    "input_tensors",
                    "response_labels_sha256",
                )
            }
            for identifier, value in encoded.items()
        }
        maximum = max(splits["train"], key=lambda row: encoded[row["sample_id"]]["total_tokens"])
        report["backward_preflight_sample_id"] = maximum["sample_id"]
        if args.mode == "preflight":
            report["backward_preflight"] = backend.backward_preflight(encoded[maximum["sample_id"]])
        else:
            holdout = splits["val"] + splits["test"]
            parent = evaluate_rows(
                backend, holdout, encoded, parent=True, artifact_directory=output / "parent-rows"
            )
            _write_json(output / "parent-evaluation.json", parent)
            report["parent_evaluation"] = parent
            report["parent_evaluation_before_optimizer"] = True
            if args.mode == "train":
                report["backward_preflight"] = backend.backward_preflight(
                    encoded[maximum["sample_id"]]
                )
                optimizer = backend.torch.optim.AdamW(
                    [
                        parameter
                        for parameter in backend.model.parameters()
                        if parameter.requires_grad
                    ],
                    lr=budget.learning_rate,
                    weight_decay=0.0,
                )
                report["optimization_rows"] = []
                shuffle = random.Random(budget.seed)
                with (output / "training-log.jsonl").open("x", encoding="utf-8") as log:
                    for epoch in range(budget.epochs):
                        rows = list(splits["train"])
                        shuffle.shuffle(rows)
                        for sample in rows:
                            optimizer.zero_grad(set_to_none=True)
                            backend.torch.cuda.reset_peak_memory_stats()
                            started = time.perf_counter()
                            loss = backend.loss(encoded[sample["sample_id"]])
                            if not backend.torch.isfinite(loss).item():
                                raise ValueError("Training loss is not finite")
                            loss.backward()
                            norm = backend.torch.nn.utils.clip_grad_norm_(
                                [p for p in backend.model.parameters() if p.requires_grad],
                                1.0,
                                error_if_nonfinite=True,
                            )
                            optimizer.step()
                            backend.torch.cuda.synchronize()
                            report["vlm_training_performed"] = True
                            report["optimizer_steps_completed"] += 1
                            entry = {
                                "step": report["optimizer_steps_completed"],
                                "epoch": epoch + 1,
                                "sample_id": sample["sample_id"],
                                "split": "train",
                                "loss": loss.detach().item(),
                                "gradient_norm_before_clip": norm.item(),
                                "elapsed_s": time.perf_counter() - started,
                                "peak_allocated_bytes": backend.torch.cuda.max_memory_allocated(),
                                "peak_reserved_bytes": backend.torch.cuda.max_memory_reserved(),
                            }
                            report["optimization_rows"].append(entry)
                            log.write(json.dumps(entry, sort_keys=True) + "\n")
                            log.flush()
                            print(json.dumps(entry, sort_keys=True), flush=True)
                backend.model.zero_grad(set_to_none=True)
                adapter = output / "adapter"
                backend.model.save_pretrained(adapter, safe_serialization=True)
                report["adapter_files"] = [
                    probe.file_record(path) for path in sorted(adapter.rglob("*")) if path.is_file()
                ]
            candidate = evaluate_rows(
                backend,
                holdout,
                encoded,
                parent=False,
                artifact_directory=output / "candidate-rows",
            )
            _write_json(output / "candidate-evaluation.json", candidate)
            report["candidate_evaluation"] = candidate
        report["base_parameter_sha256_after"] = parameter_digest(backend.model, trainable=False)
        report["adapter_parameter_sha256_after"] = parameter_digest(backend.model, trainable=True)
        if report["base_parameter_sha256_before"] != report["base_parameter_sha256_after"]:
            raise ValueError("Actual frozen base/vision tensor bytes changed")
        report["base_parameter_bytes_unchanged"] = True
        audit_trainable_parameters(backend.model)
        if args.mode == "preflight" and (
            report["adapter_parameter_sha256_before"] != report["adapter_parameter_sha256_after"]
        ):
            raise ValueError("Backward-only preflight unexpectedly changed adapter weights")
        if args.mode == "train" and (
            report["adapter_parameter_sha256_before"] == report["adapter_parameter_sha256_after"]
        ):
            raise ValueError("Optimizer did not change actual adapter weights")
        if args.mode == "train":
            probe.verify_records(report["adapter_files"])
            representative = splits["val"][0]
            recorded = next(
                row
                for row in report["candidate_evaluation"]["samples"]
                if row["sample_id"] == representative["sample_id"]
            )
            report["saved_adapter_reload"] = backend.verify_saved_adapter(
                output / "adapter",
                encoded[representative["sample_id"]],
                recorded,
            )
            report["saved_adapter_reload"]["sample_id"] = representative["sample_id"]
            report["base_parameter_sha256_after_reload"] = parameter_digest(
                backend.model, trainable=False
            )
            if (
                report["base_parameter_sha256_after_reload"]
                != report["base_parameter_sha256_before"]
            ):
                raise ValueError("Offline adapter reload changed actual base/vision tensor bytes")
        probe.verify_records(
            sources
            + report["runtime_files"]
            + provenance["files"]
            + [invocation_record]
            + report.get("adapter_files", [])
        )
        dataset_validator(
            args.dataset_manifest,
            args.dataset_sha256,
            review_receipt=args.review_receipt,
            require_review=True,
        )
        report["source_bytes_reverified"] = True
        report["native_exit_code"] = 0
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        report["failure"] = str(exc)
    report["output_artifact_files"] = [
        probe.file_record(path) for path in sorted(output.rglob("*")) if path.is_file()
    ]
    probe.verify_records(report["output_artifact_files"])
    _write_json(output / "report.json", report)
    return report["native_exit_code"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "train"), default="preflight")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--dataset-manifest", type=Path, required=True)
    parser.add_argument("--dataset-sha256", required=True)
    parser.add_argument("--review-receipt", type=Path, required=True)
    parser.add_argument("--model-id", default=MODEL_ID)
    parser.add_argument("--revision", default=REVISION)
    parser.add_argument("--cache-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--max-optimizer-steps", type=int, default=200)
    parser.add_argument("--max-pixels-per-image", type=int, default=200704)
    parser.add_argument("--max-input-tokens", type=int, default=4096)
    parser.add_argument("--max-new-tokens", type=int, default=192)
    parser.add_argument("--rank", type=int, default=4)
    parser.add_argument("--alpha", type=int, default=8)
    parser.add_argument("--learning-rate", type=float, default=0.0001)
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--no-gradient-checkpointing", action="store_true")
    try:
        return run(parser.parse_args(argv))
    except Exception as exc:
        print(f"Grounding trainer refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
