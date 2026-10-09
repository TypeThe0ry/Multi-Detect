"""Reviewed, train-only planned-navigation Qwen LoRA pilot; never flight authority.

All heavy dependencies are lazy. A bound plan and separate source-review receipt
are required before loading the existing local base. No weights are downloaded.
Probe results are resubstitution diagnostics, not independent task accuracy.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import io
import json
import math
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multidetect.autonomy import onboard_navigation_supervision as navigation
from scripts import train_onboard_vlm_grounding as core
from scripts import verify_onboard_vlm_grounding_adapter as recovery

CONTRACT = "multidetect.onboard-vlm-navigation-pilot-training-v1"
PLAN_CONTRACT = "multidetect.onboard-vlm-navigation-pilot-plan-v1"
REVIEW_CONTRACT = "multidetect.onboard-navigation-pilot-training-review-v1"
BASE_SHA256 = "7de1838c87a5349b016c26a1c3f7d2bc400a3d485f95ef39a7059ffd734977a0"
BASE_BYTES = 4_255_140_312
EXPECTED_SAMPLES = 86
SOURCE_PATHS = (
    "scripts/train_onboard_vlm_navigation_pilot.py",
    "tests/test_train_onboard_vlm_navigation_pilot.py",
    "scripts/train_onboard_vlm_grounding.py",
    "scripts/verify_onboard_vlm_grounding_adapter.py",
    "scripts/probe_onboard_vlm.py",
    "scripts/build_onboard_openfly_navigation_dataset.py",
    "src/multidetect/autonomy/onboard_navigation_supervision.py",
    "src/multidetect/autonomy/serialization.py",
)
CLAIMS = {
    "resubstitution_only": True,
    "source_planned_not_executed": True,
    "quality_claim": False,
    "candidate_promoted": False,
    "physical_performance_claimed": False,
    "hil_real_evidence": False,
    "outdoor_gnss_evidence": False,
    "final_release_allowed": False,
}
TARGET_FIELDS = {
    "schema_version",
    "contract",
    "kind",
    "translation_frame",
    "yaw_convention",
    "translation_m",
    "yaw_delta_rad",
}


@dataclass(frozen=True)
class NavigationTrainingBudget(core.TrainingBudget):
    max_optimizer_steps: int = 100
    max_pixels_per_image: int = 50176
    learning_rate: float = 2e-5
    gradient_clip_norm: float = 1.0
    max_cuda_reserved_bytes: int = 7_680 * 1024 * 1024

    def validate(self):
        # This is one frozen pilot, not a generic hyperparameter search surface.
        if asdict(self) != asdict(NavigationTrainingBudget()):
            raise ValueError("Optimization differs from the bounded navigation pilot")
        super().validate()


def strict_json(raw):
    return core.strict_factual_json(raw if isinstance(raw, bytes) else raw.encode("utf-8"))


def exact_object(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"Unexpected {label} fields")
    return value


def binding_shape(value):
    exact_object(value, {"path", "bytes", "sha256"}, "file binding")
    if not isinstance(value["path"], str) or not value["path"]:
        raise ValueError("File binding needs a path")
    if type(value["bytes"]) is not int or value["bytes"] <= 0:
        raise ValueError("File binding needs positive bytes")
    core.probe.require_sha256(value["sha256"])
    return value


def verify_binding(value, root):
    binding_shape(value)
    root = Path(root).resolve(strict=True)
    raw_path = Path(value["path"])
    path = (raw_path if raw_path.is_absolute() else root / raw_path).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("A planned source escaped artifact_root")
    if path.stat().st_size != value["bytes"] or core.probe.sha256_file(path) != value["sha256"]:
        raise ValueError("Bound source bytes or SHA changed")
    return path


def read_bound_json(value, root, maximum=32 * 1024 * 1024):
    if value["bytes"] > maximum:
        raise ValueError("Bound JSON exceeds its cap")
    return strict_json(verify_binding(value, root).read_bytes())


def validate_plan_shape(plan):
    exact_object(
        plan,
        {
            "schema_version",
            "contract",
            "artifact_root",
            "dataset_report",
            "dataset_samples",
            "review_receipt",
            "source_files",
            "base",
            "optimization",
            "expected_samples",
            "train_sample_ids",
            "diagnostic_probes",
            "claims",
        },
        "plan",
    )
    if (
        type(plan["schema_version"]) is not int
        or plan["schema_version"] != 1
        or plan["contract"] != PLAN_CONTRACT
    ):
        raise ValueError("Unsupported navigation plan")
    if not isinstance(plan["artifact_root"], str) or not Path(plan["artifact_root"]).is_absolute():
        raise ValueError("artifact_root must be an absolute path")
    for name in ("dataset_report", "dataset_samples", "review_receipt"):
        binding_shape(plan[name])
    if not isinstance(plan["source_files"], list):
        raise ValueError("Source files must be bound")
    for value in plan["source_files"]:
        binding_shape(value)
    root = Path(plan["artifact_root"])
    paths = {
        (Path(v["path"]) if Path(v["path"]).is_absolute() else root / v["path"]).as_posix()
        for v in plan["source_files"]
    }
    if len(paths) != len(plan["source_files"]) or not all(
        (root / relative).as_posix() in paths for relative in SOURCE_PATHS
    ):
        raise ValueError("Plan lacks unique trainer/core/contract/converter source bindings")
    base = exact_object(
        plan["base"], {"model_id", "revision", "snapshot", "safetensors", "processor_files"}, "base"
    )
    binding_shape(base["safetensors"])
    if (
        base["model_id"],
        base["revision"],
        base["safetensors"]["bytes"],
        base["safetensors"]["sha256"],
    ) != (core.MODEL_ID, core.REVISION, BASE_BYTES, BASE_SHA256):
        raise ValueError("Only the existing pinned Qwen base is admitted")
    if not isinstance(base["snapshot"], str) or Path(base["snapshot"]).name != core.REVISION:
        raise ValueError("Base snapshot must be explicit and pinned")
    if (
        Path(base["safetensors"]["path"]).name != "model.safetensors"
        or not isinstance(base["processor_files"], list)
        or not base["processor_files"]
    ):
        raise ValueError("Base/processor files are not explicit")
    for value in base["processor_files"]:
        binding_shape(value)
    budget = NavigationTrainingBudget(
        **exact_object(plan["optimization"], asdict(NavigationTrainingBudget()), "optimization")
    )
    budget.validate()
    if type(plan["expected_samples"]) is not int or plan["expected_samples"] != EXPECTED_SAMPLES:
        raise ValueError("This pilot must use all 86 reviewed source steps")
    ids = plan["train_sample_ids"]
    if (
        not isinstance(ids, list)
        or len(ids) != EXPECTED_SAMPLES
        or not all(isinstance(v, str) and v for v in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("All source sample IDs must be unique and predeclared")
    probes = plan["diagnostic_probes"]
    if not isinstance(probes, list) or not 1 <= len(probes) <= 4:
        raise ValueError("Predeclare one to four training probes")
    for probe in probes:
        exact_object(probe, {"sample_id", "action_class"}, "diagnostic probe")
        if probe["sample_id"] not in ids or probe["action_class"] not in {
            "forward",
            "left",
            "right",
            "stop",
        }:
            raise ValueError("Probe must be a declared training source/action class")
    if len({p["sample_id"] for p in probes}) != len(probes) or len(
        {p["action_class"] for p in probes}
    ) != len(probes):
        raise ValueError("Probe sample IDs/action classes must not repeat")
    if (
        not isinstance(plan["claims"], dict)
        or set(plan["claims"]) != set(CLAIMS)
        or any(plan["claims"][name] is not value for name, value in CLAIMS.items())
    ):
        raise ValueError("A train-only pilot cannot claim independent quality or execution")
    return budget


def load_plan(path, expected_sha256):
    core.probe.require_sha256(expected_sha256)
    raw = Path(path).read_bytes()
    if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Plan SHA or size differs from the frozen invocation")
    plan = strict_json(raw)
    validate_plan_shape(plan)
    return plan


def plan_bindings(plan):
    return [
        plan["dataset_report"],
        plan["dataset_samples"],
        plan["review_receipt"],
        *plan["source_files"],
        plan["base"]["safetensors"],
        *plan["base"]["processor_files"],
    ]


def verify_plan_sources(plan):
    root = Path(plan["artifact_root"])
    for value in plan_bindings(plan):
        verify_binding(value, root)
    snapshot = Path(plan["base"]["snapshot"]).resolve(strict=True)
    if verify_binding(plan["base"]["safetensors"], root).parent != snapshot or any(
        verify_binding(v, root).parent != snapshot for v in plan["base"]["processor_files"]
    ):
        raise ValueError("Model/processor files must share the explicit local snapshot")
    actual_files = {path.resolve(strict=True) for path in snapshot.rglob("*") if path.is_file()}
    expected_files = {
        verify_binding(v, root)
        for v in [plan["base"]["safetensors"], *plan["base"]["processor_files"]]
    }
    if actual_files != expected_files or len(expected_files) != 1 + len(
        plan["base"]["processor_files"]
    ):
        raise ValueError(
            "Snapshot full file set differs; every config/tokenizer/template must be bound"
        )


def admit_dataset(plan):
    root = Path(plan["artifact_root"])
    receipt = read_bound_json(plan["review_receipt"], root)
    required = {
        "schema_version",
        "contract",
        "dataset_report_sha256",
        "dataset_samples_sha256",
        "allowed_training_use",
        "optimizer_allowed",
        "independent_quality_evidence",
    }
    if (
        not isinstance(receipt, dict)
        or not required <= set(receipt)
        or set(receipt) - required - {"reviewer", "reviewer_evidence"}
    ):
        raise ValueError("Unexpected source training review receipt")
    if (
        type(receipt["schema_version"]) is not int
        or (
            receipt["schema_version"],
            receipt["contract"],
            receipt["dataset_report_sha256"],
            receipt["dataset_samples_sha256"],
            receipt["allowed_training_use"],
        )
        != (
            1,
            REVIEW_CONTRACT,
            plan["dataset_report"]["sha256"],
            plan["dataset_samples"]["sha256"],
            "planned_navigation_train_only_resubstitution_pilot",
        )
        or receipt["optimizer_allowed"] is not True
        or receipt["independent_quality_evidence"] is not False
    ):
        raise ValueError("Independent receipt does not approve this exact bounded training use")
    report = read_bound_json(plan["dataset_report"], root)
    if (
        report.get("contract") != "multidetect.openfly-navigation-dataset-report-v1"
        or report.get("admitted_sample_count") != EXPECTED_SAMPLES
        or report.get("source_planned_not_executed") is not True
        or report.get("pose_future_truth_or_label_in_actor_input") is not False
        or report.get("aggregate_annotation_actions_used_as_gold") is not False
    ):
        raise ValueError("Dataset report lacks reviewed original-step supervision")
    if report["dataset"]["sha256"] != plan["dataset_samples"]["sha256"] or verify_binding(
        report["dataset"], root
    ) != verify_binding(plan["dataset_samples"], root):
        raise ValueError("Report/sample file identities differ")
    path = verify_binding(plan["dataset_samples"], root)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Pilot samples file exceeds its cap")
    with path.open("rb") as stream:
        samples = tuple(
            navigation.NavigationSupervisionSample.from_json(line)
            for line in stream
            if line.strip()
        )
    navigation.validate_navigation_episode_samples(samples)
    if [sample.sample_id for sample in samples] != plan["train_sample_ids"] or any(
        sample.partition != "train" or sample.source.original_split != "train" for sample in samples
    ):
        raise ValueError("Train-only complete source identities/order differ from plan")
    if len(samples) > NavigationTrainingBudget().max_optimizer_steps:
        raise ValueError("No epoch truncation is allowed")
    # Validate source/labels together once; no optimizer may use provisional session data.
    with navigation.NavigationSourceValidationSession(root) as session:
        session.prime(samples)
        targets = {
            sample.sample_id: strict_json(
                navigation.serialize_navigation_supervision_target(sample, root, session=session)
            )
            for sample in samples
        }
    if not session.verified:
        raise ValueError("Original source session did not finish its final hash check")
    for probe in plan["diagnostic_probes"]:
        first = next(
            (
                s.sample_id
                for s in samples
                if action_class(targets[s.sample_id]) == probe["action_class"]
            ),
            None,
        )
        if first != probe["sample_id"]:
            raise ValueError("Probe selection must be the first source sample in its action class")
    return samples, targets


def tensor_record(tensor, torch):
    return {
        "shape": list(tensor.shape),
        "dtype": str(tensor.dtype),
        "sha256": hashlib.sha256(
            tensor.detach().cpu().contiguous().view(torch.uint8).numpy().tobytes()
        ).hexdigest(),
    }


class NavigationBackend(core.GroundingBackend):
    def __init__(self, snapshot, budget):
        import torch

        if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
            raise RuntimeError("Native BF16-compatible CUDA is required; no CPU/offload fallback")
        free, total = torch.cuda.mem_get_info()
        self.pre_load_cuda_memory = {
            "device_name": torch.cuda.get_device_name(),
            "device_free_bytes": free,
            "device_total_bytes": total,
            "device_used_bytes": total - free,
            "this_process_reserved_bytes": torch.cuda.memory_reserved(),
            "other_usage_estimate_bytes": max(0, total - free - torch.cuda.memory_reserved()),
            "other_usage_is_estimate_not_process_attribution": True,
            "external_processes_stopped": False,
            "pytorch_reserved_cap_bytes": budget.max_cuda_reserved_bytes,
        }
        print(
            json.dumps(
                {"event": "navigation_pilot_before_model_load", **self.pre_load_cuda_memory}
            ),
            flush=True,
        )
        try:
            super().__init__(snapshot, budget)
        except Exception as exc:
            exc.pre_load_cuda_memory = self.pre_load_cuda_memory
            raise
        if self.trainable_audit["trainable_dtypes"] != ["torch.float32"] or self.trainable_audit[
            "frozen_base_dtypes"
        ] != ["torch.bfloat16"]:
            raise ValueError("Native BF16 frozen base and FP32 q/v adapters are required")

    def encode(self, sample, artifact_root, *, session):
        messages = navigation.serialize_navigation_supervision_messages(
            sample, artifact_root, session=session
        )
        target = navigation.serialize_navigation_supervision_target(
            sample, artifact_root, session=session
        )
        encoded = self.encode_messages(messages, target)
        encoded["source_prompt_sha256"] = sample.prompt.utf8_sha256
        encoded["source_rgb_frames"] = [
            {"step_index": f.step_index, "sha256": f.sha256, "width": f.width, "height": f.height}
            for f in sample.frames
        ]
        return encoded

    def encode_messages(self, messages, target):
        torch = self.torch
        image_count = sum(item.get("type") == "image" for m in messages for item in m["content"])
        if image_count not in (1, 2):
            raise ValueError("Exactly current plus at most one past real RGB is required")

        def process(chat, generation):
            # New nested dict each call: the official processor consumes this mapping.
            return self.processor.apply_chat_template(
                chat,
                add_generation_prompt=generation,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
                images_kwargs={"min_pixels": 1024, "max_pixels": self.budget.max_pixels_per_image},
            )

        prefix = process(messages, True)
        full = process(
            messages + [{"role": "assistant", "content": [{"type": "text", "text": target}]}], False
        )
        fields = {"input_ids", "attention_mask", "pixel_values", "image_grid_thw"}
        if set(prefix) != fields or set(full) != fields:
            raise ValueError("Unexpected processor tensors; image-only input is required")
        grids = full["image_grid_thw"].tolist()
        patch = self.processor.image_processor.patch_size
        if (
            type(patch) is not int
            or patch <= 0
            or len(grids) != image_count
            or any(
                len(g) != 3
                or any(type(v) is not int or v <= 0 for v in g)
                or g[0] != 1
                or g[1] * g[2] * patch**2 > self.budget.max_pixels_per_image
                for g in grids
            )
        ):
            raise ValueError("Actual image_grid_thw exceeds the per-image pixel budget")
        if (
            not torch.equal(full["image_grid_thw"], prefix["image_grid_thw"])
            or not torch.equal(full["pixel_values"], prefix["pixel_values"])
            or not torch.isfinite(full["pixel_values"]).all().item()
        ):
            raise ValueError("Actual prefix/full visual tensor identity or finiteness failed")
        if full["pixel_values"].ndim != 2 or full["pixel_values"].shape[0] != sum(
            t * h * w for t, h, w in grids
        ):
            raise ValueError("Actual patch tensor count differs from image_grid_thw")
        if (
            full["input_ids"].ndim != 2
            or full["input_ids"].shape[0] != 1
            or prefix["input_ids"].ndim != 2
            or prefix["input_ids"].shape[0] != 1
        ):
            raise ValueError("Exactly one sample per processor batch is required")
        full_ids, prefix_ids = full["input_ids"][0].tolist(), prefix["input_ids"][0].tolist()
        if (
            len(full_ids) > self.budget.max_input_tokens
            or len(full_ids) - len(prefix_ids) > self.budget.max_new_tokens
        ):
            raise ValueError("Actual tokens exceed the frozen cap; no truncation")
        tokens = {
            self.processor.tokenizer.convert_tokens_to_ids(v)
            for v in ("<|image_pad|>", "<|video_pad|>", "<|vision_start|>", "<|vision_end|>")
        }
        labels = core.response_only_labels(
            full_ids, prefix_ids, full["attention_mask"][0].tolist(), tokens
        )
        if not torch.equal(prefix["attention_mask"], full["attention_mask"][:, : len(prefix_ids)]):
            raise ValueError("Generation prefix attention mask differs")
        return {
            "prefix": prefix,
            "full": full,
            "labels": torch.tensor([labels], dtype=torch.long),
            "prefix_tokens": len(prefix_ids),
            "total_tokens": len(full_ids),
            "response_tokens": len(full_ids) - len(prefix_ids),
            "supervised_tokens": sum(v != -100 for v in labels),
            "actual_image_grid_thw": grids,
            "actual_image_pixels": [h * w * patch**2 for _, h, w in grids],
            "processor_image_limits": {
                "min_pixels": 1024,
                "max_pixels": self.budget.max_pixels_per_image,
            },
            "prefix_tensors": {k: tensor_record(v, torch) for k, v in prefix.items()},
            "input_tensors": {k: tensor_record(v, torch) for k, v in full.items()},
            "response_labels_sha256": hashlib.sha256(core.canonical_bytes(labels)).hexdigest(),
            "target_sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
        }


def action_class(target):
    validate_target(target)
    if target["kind"] == "stop":
        return "stop"
    if target["kind"] == "turn_yaw":
        if target["yaw_delta_rad"] is None:
            return "unknown_turn"
        return (
            "left"
            if target["yaw_delta_rad"] < 0
            else "right"
            if target["yaw_delta_rad"] > 0
            else "zero_turn"
        )
    if target["translation_m"] is None:
        return "unknown_translation"
    f, r, d = target["translation_m"]
    if r == 0 and d == 0 and f > 0:
        return "forward"
    return "unprobed_translation"


def validate_target(value):
    exact_object(value, TARGET_FIELDS, "planned navigation target")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["contract"] != navigation.TARGET_CONTRACT
        or value["translation_frame"] != "body_frd"
        or value["yaw_convention"] != "clockwise_right_positive"
        or value["kind"] not in {"translate", "turn_yaw", "stop"}
    ):
        raise ValueError("Unsupported planned navigation response")

    def number(v):
        return type(v) in (int, float) and math.isfinite(v)

    if value["kind"] == "translate":
        if (
            value["yaw_delta_rad"] is not None
            or value["translation_m"] is not None
            and (
                not isinstance(value["translation_m"], list)
                or len(value["translation_m"]) != 3
                or not all(number(v) for v in value["translation_m"])
                or not any(value["translation_m"])
                or any(abs(v) > 500 for v in value["translation_m"])
            )
        ):
            raise ValueError("Translation response needs a finite FRD vector only")
    elif value["kind"] == "turn_yaw":
        if (
            value["translation_m"] is not None
            or value["yaw_delta_rad"] is not None
            and (
                not number(value["yaw_delta_rad"])
                or abs(value["yaw_delta_rad"]) > math.pi
                or value["yaw_delta_rad"] == 0
            )
        ):
            raise ValueError("Turn response needs a finite bounded yaw only")
    elif value["translation_m"] is not None or value["yaw_delta_rad"] is not None:
        raise ValueError("STOP is not movement, landing or task completion")


def diagnose(raw_output, target):
    result = {
        "strict_json_valid": False,
        "action_class_correct": False,
        "metric_correct": False,
        "quality_claim": False,
        "resubstitution_only": True,
    }
    try:
        output = strict_json(raw_output)
        validate_target(output)
        result["strict_json_valid"] = True
        result["predicted_action_class"] = action_class(output)
        result["action_class_correct"] = action_class(output) == action_class(target)
        if (
            output["kind"] == target["kind"] == "translate"
            and output["translation_m"] is not None
            and target["translation_m"] is not None
        ):
            error = math.sqrt(
                sum(
                    (a - b) ** 2
                    for a, b in zip(output["translation_m"], target["translation_m"], strict=True)
                )
            )
            result["translation_l2_error_m"] = error
            result["metric_correct"] = error <= 1e-4
        elif (
            output["kind"] == target["kind"] == "turn_yaw"
            and output["yaw_delta_rad"] is not None
            and target["yaw_delta_rad"] is not None
        ):
            error = abs(output["yaw_delta_rad"] - target["yaw_delta_rad"])
            result["yaw_absolute_error_rad"] = error
            result["metric_correct"] = error <= 1e-6
        elif output["kind"] == target["kind"] == "stop":
            result["metric_correct"] = True
    except (ValueError, TypeError, KeyError):
        result["rejection"] = "not_a_strict_supported_planned_navigation_target"
    return result


def create_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(core.canonical_bytes(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def append_event(stream, value):
    stream.write(json.dumps(value, allow_nan=False, sort_keys=True) + "\n")
    stream.flush()
    if not isinstance(stream, io.StringIO):
        os.fsync(stream.fileno())
    print(json.dumps(value, allow_nan=False, sort_keys=True), flush=True)


def check_memory(backend, budget):
    allocated = backend.torch.cuda.max_memory_allocated()
    reserved = backend.torch.cuda.max_memory_reserved()
    if allocated > budget.max_cuda_reserved_bytes or reserved > budget.max_cuda_reserved_bytes:
        raise ValueError("Actual CUDA peak exceeded the predeclared 8GB-device pilot cap")
    free, total = backend.torch.cuda.mem_get_info()
    reserved_now = backend.torch.cuda.memory_reserved()
    return {
        "peak_allocated_bytes": allocated,
        "peak_reserved_bytes": reserved,
        "device_free_bytes": free,
        "device_total_bytes": total,
        "this_process_reserved_bytes": reserved_now,
        "other_usage_estimate_bytes": max(0, total - free - reserved_now),
        "other_usage_is_estimate_not_process_attribution": True,
    }


def finite_gradients(backend):
    total = 0.0
    for name, parameter in backend.model.named_parameters():
        if not parameter.requires_grad and parameter.grad is not None:
            raise ValueError(f"Frozen parameter received a gradient: {name}")
        if parameter.requires_grad and parameter.grad is not None:
            if not backend.torch.isfinite(parameter.grad).all().item():
                raise ValueError("Non-finite language adapter gradient")
            total += parameter.grad.detach().float().abs().sum().item()
    if total <= 0:
        raise ValueError("No nonzero actual adapter gradient")
    return total


def train_epoch(backend, samples, encoded, budget, report, stream):
    budget.validate()
    if len(samples) != EXPECTED_SAMPLES or len(samples) > budget.max_optimizer_steps:
        raise ValueError("Complete reviewed epoch must fit; no truncation")
    parameters = [p for p in backend.model.parameters() if p.requires_grad]
    optimizer = backend.torch.optim.AdamW(parameters, lr=budget.learning_rate, weight_decay=0.0)
    rows = list(samples)
    random.Random(budget.seed).shuffle(rows)
    for step, sample in enumerate(rows, 1):
        optimizer.zero_grad(set_to_none=True)
        backend.torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        loss = backend.loss(encoded[sample.sample_id])
        if not backend.torch.isfinite(loss).item():
            raise ValueError("Non-finite actual training loss")
        report["optimization_backward_attempted"] = True
        loss.backward()
        report["forward_backward_performed"] = True
        gradient_sum = finite_gradients(backend)
        norm = backend.torch.nn.utils.clip_grad_norm_(
            parameters, budget.gradient_clip_norm, error_if_nonfinite=True
        )
        backend.torch.cuda.synchronize()
        memory = check_memory(backend, budget)
        common = {"step": step, "sample_id": sample.sample_id, "split": "train", "epoch": 1}
        report["optimizer_steps_attempted"] += 1
        append_event(
            stream,
            {
                **common,
                "stage": "optimizer_step_attempted",
                "loss": loss.detach().item(),
                "gradient_absolute_sum": gradient_sum,
                "gradient_norm_before_clip": norm.item(),
                **memory,
            },
        )
        optimizer.step()
        report["optimizer_steps_returned"] += 1
        append_event(stream, {**common, "stage": "optimizer_step_returned"})
        backend.torch.cuda.synchronize()
        report["optimizer_steps_synchronized"] += 1
        append_event(stream, {**common, "stage": "optimizer_step_synchronized"})
        memory = check_memory(backend, budget)
        report["optimizer_steps_completed"] += 1
        append_event(
            stream,
            {
                **common,
                "stage": "optimizer_step_completed",
                "elapsed_s": time.perf_counter() - started,
                **memory,
            },
        )
    backend.model.zero_grad(set_to_none=True)


def evaluate_probes(backend, probes, encoded, targets, budget, *, parent):
    result = []
    for probe in probes:
        value = encoded[probe["sample_id"]]
        generated = backend.generate(value, parent=parent)
        memory = check_memory(backend, budget)
        result.append(
            {
                **probe,
                "parent": parent,
                "generated": generated,
                "actual_prefix_tensors": value["prefix_tensors"],
                "diagnostic": diagnose(generated["raw_output"], targets[probe["sample_id"]]),
                **memory,
            }
        )
    return result


def pre_optimizer_admission(plan, plan_path, plan_sha256, samples):
    """Review revocation/source drift after parent inference cannot reach optimizer."""
    verify_plan_sources(plan)
    if core.probe.sha256_file(Path(plan_path)) != plan_sha256:
        raise ValueError("Frozen plan changed before optimizer admission")
    with navigation.NavigationSourceValidationSession(Path(plan["artifact_root"])) as session:
        session.prime(samples)
        for sample in samples:
            session.validate_sample(sample)
    if not session.verified:
        raise ValueError("Pre-optimizer original-source validation did not finish")


def record_failure(report, exc):
    report["failure_type"], report["failure"] = type(exc).__name__, str(exc)
    report["optimizer_update_uncertain"] = (
        len(
            {
                report[key]
                for key in (
                    "optimizer_steps_attempted",
                    "optimizer_steps_returned",
                    "optimizer_steps_synchronized",
                    "optimizer_steps_completed",
                )
            }
        )
        != 1
    )
    report["adapter_acceptance_uncertain"] = report["optimizer_steps_attempted"] > 0
    if hasattr(exc, "pre_load_cuda_memory"):
        report["pre_load_cuda_memory"] = exc.pre_load_cuda_memory


def run(args, *, backend_factory=NavigationBackend):
    output = Path(args.output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("Pilot output must be a fresh create-only directory")
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        **CLAIMS,
        "native_exit_code": 1,
        "optimizer_steps_attempted": 0,
        "optimizer_steps_returned": 0,
        "optimizer_steps_synchronized": 0,
        "optimizer_steps_completed": 0,
        "forward_backward_performed": False,
        "transport_created": False,
        "mission_created": False,
        "execution_authority": False,
        "independent_holdout_available": False,
        "unavailable_diagnostic_action_classes": ["up", "down"],
        "training_plan_sha256": args.plan_sha256,
    }
    try:
        plan = load_plan(args.plan, args.plan_sha256)
        root, budget = Path(plan["artifact_root"]).resolve(strict=True), validate_plan_shape(plan)
        if not output.resolve().is_relative_to(root):
            raise ValueError("Output escaped the declared artifact_root")
        verify_plan_sources(plan)
        samples, targets = admit_dataset(plan)
        invocation = {
            "plan": core.probe.file_record(Path(args.plan)),
            "output_dir": str(output),
            "argv": [
                "--plan",
                str(args.plan),
                "--plan-sha256",
                args.plan_sha256,
                "--output-dir",
                str(output),
            ],
        }
        create_json(output / "invocation.json", invocation)
        backend = backend_factory(Path(plan["base"]["snapshot"]), budget)
        report.update(
            {
                "optimization": asdict(budget),
                "trainable_audit": backend.trainable_audit,
                "base_parameter_sha256_before": backend.base_digest_initial,
                "adapter_parameter_sha256_before": backend.adapter_digest_initial,
                "runtime_files": backend.runtime_records(),
                "model_id": core.MODEL_ID,
                "revision": core.REVISION,
                "declared_train_sample_ids": plan["train_sample_ids"],
                "pre_load_cuda_memory": backend.pre_load_cuda_memory,
            }
        )
        with navigation.NavigationSourceValidationSession(root) as session:
            session.prime(samples)
            encoded = {
                sample.sample_id: backend.encode(sample, root, session=session)
                for sample in samples
            }
        if not session.verified:
            raise ValueError("Encoder source validation did not finish")
        create_json(
            output / "prepared-inputs.json",
            {
                key: {k: v for k, v in value.items() if k not in {"prefix", "full", "labels"}}
                for key, value in encoded.items()
            },
        )
        maximum = max(samples, key=lambda sample: encoded[sample.sample_id]["total_tokens"])
        report["backward_preflight_sample_id"] = maximum.sample_id
        report["backward_preflight"] = backend.backward_preflight(encoded[maximum.sample_id])
        check_memory(backend, budget)
        if core.parameter_digest(backend.model, trainable=True) != backend.adapter_digest_initial:
            raise ValueError("Backward-only preflight changed adapter parameters")
        parent = evaluate_probes(
            backend, plan["diagnostic_probes"], encoded, targets, budget, parent=True
        )
        create_json(output / "parent-probes.json", parent)
        pre_optimizer_admission(plan, args.plan, args.plan_sha256, samples)
        report["pre_optimizer_receipt_and_source_reverified"] = True
        with (output / "training-events.jsonl").open("x", encoding="utf-8") as stream:
            train_epoch(backend, samples, encoded, budget, report, stream)
        report["base_parameter_sha256_after"] = core.parameter_digest(
            backend.model, trainable=False
        )
        report["adapter_parameter_sha256_after"] = core.parameter_digest(
            backend.model, trainable=True
        )
        if (
            report["base_parameter_sha256_after"] != report["base_parameter_sha256_before"]
            or report["adapter_parameter_sha256_after"] == report["adapter_parameter_sha256_before"]
        ):
            raise ValueError("Base changed or adapter did not change")
        report["base_parameter_bytes_unchanged"] = True
        core.audit_trainable_parameters(backend.model)
        adapter = output / "adapter"
        backend.model.save_pretrained(adapter, safe_serialization=True)
        report["adapter_files"] = [
            core.probe.file_record(p) for p in sorted(adapter.rglob("*")) if p.is_file()
        ]
        from safetensors.torch import load_file

        config = strict_json((adapter / "adapter_config.json").read_bytes())
        recovery.verify_config(config, budget)
        state = load_file(str(adapter / "adapter_model.safetensors"), device="cpu")
        if (
            recovery.saved_parameter_digest(state, report["trainable_audit"], rank=budget.rank)
            != report["adapter_parameter_sha256_after"]
        ):
            raise ValueError("Saved adapter tensors do not reproduce trained tensor identity")
        candidate = evaluate_probes(
            backend, plan["diagnostic_probes"], encoded, targets, budget, parent=False
        )
        create_json(output / "candidate-probes.json", candidate)
        # Release the first GPU model before loading an independent explicit base.
        del backend.model
        gc.collect()
        backend.torch.cuda.empty_cache()
        fresh = backend_factory(Path(plan["base"]["snapshot"]), budget)
        if fresh.base_digest_initial != report["base_parameter_sha256_before"]:
            raise ValueError("Fresh base bytes differ from the frozen original base")
        reloaded = recovery.load_saved_exactly(
            fresh.model,
            state,
            report["trainable_audit"],
            report["adapter_parameter_sha256_after"],
            rank=budget.rank,
        )
        for original in candidate:
            generated = fresh.generate(encoded[original["sample_id"]], parent=False)
            check_memory(fresh, budget)
            if generated["output_token_ids"] != original["generated"]["output_token_ids"]:
                raise ValueError("Fresh exact adapter reload changed probe generation tokens")
        if (
            core.parameter_digest(fresh.model, trainable=False)
            != report["base_parameter_sha256_before"]
        ):
            raise ValueError("Fresh reload changed frozen base bytes")
        report["saved_adapter_reload"] = {
            **reloaded,
            "fresh_pinned_base_loaded": True,
            "probe_generation_token_ids_equal": True,
            "optimizer_performed_during_reload": False,
        }
        verify_plan_sources(plan)
        if core.probe.sha256_file(Path(args.plan)) != args.plan_sha256:
            raise ValueError("Plan changed during training")
        core.probe.verify_records(report["runtime_files"] + report["adapter_files"])
        with navigation.NavigationSourceValidationSession(root) as final_session:
            final_session.prime(samples)
            for sample in samples:
                final_session.validate_sample(sample)
        report["source_bytes_reverified"] = final_session.verified
        report["native_exit_code"] = 0
    except Exception as exc:
        record_failure(report, exc)
    report["optimizer_performed"] = (
        True
        if report["optimizer_steps_returned"]
        else None
        if report["optimizer_steps_attempted"]
        else False
    )
    report.setdefault("optimizer_update_uncertain", False)
    report.setdefault("adapter_acceptance_uncertain", False)
    report["output_artifact_files"] = [
        core.probe.file_record(p) for p in sorted(output.rglob("*")) if p.is_file()
    ]
    create_json(output / "report.json", report)
    print(
        json.dumps(
            {
                "event": "navigation_pilot_finished",
                "native_exit_code": report["native_exit_code"],
                "optimizer_steps_completed": report["optimizer_steps_completed"],
                "quality_claim": False,
                "candidate_promoted": False,
            }
        ),
        flush=True,
    )
    return report["native_exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "navigation_pilot_invocation_failed",
                    "failure_type": type(exc).__name__,
                    "native_exit_code": 1,
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
