"""CPU prefix equivalence on retained supervision RGB; not live or model parity."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN_SHA256 = "f4d76902c220812201f2ca4e961b0f427e4378ba6ee5449bf96e86be23999c53"
CONTRACT = "multidetect.onboard-navigation-actor-prefix-verification-v1"
FIELDS = {"input_ids", "attention_mask", "pixel_values", "image_grid_thw"}
MAX_PIXELS = 50176
MIN_PIXELS = 1024
MAX_TOKENS = 4096


@contextmanager
def repository_imports(root):
    original = sys.path[:]
    try:
        for path in (root, root / "src"):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        yield
    finally:
        sys.path[:] = original


def validate_prefix_inputs(
    inputs,
    *,
    processor,
    image_count,
    torch_module,
    tensor_record,
    max_pixels=MAX_PIXELS,
    max_tokens=MAX_TOKENS,
):
    if type(max_pixels) is not int or max_pixels != MAX_PIXELS:
        raise ValueError("Image processing must keep the declared 50176-pixel cap")
    if type(max_tokens) is not int or max_tokens != MAX_TOKENS:
        raise ValueError("Prefix processing must keep the declared 4096-token cap")
    if type(image_count) is not int or image_count not in (1, 2):
        raise ValueError("Exactly current and at most latest past RGB are required")
    if set(inputs) != FIELDS:
        raise ValueError("Unexpected processor tensor fields")
    torch = torch_module
    for tensor in inputs.values():
        if not isinstance(tensor, torch.Tensor) or tensor.device.type != "cpu":
            raise ValueError("All processor outputs must be actual CPU tensors")
        if tensor.requires_grad:
            raise ValueError("CPU input verification cannot retain an autograd graph")
    ids, mask, pixels, grid = (
        inputs[key] for key in ("input_ids", "attention_mask", "pixel_values", "image_grid_thw")
    )
    if (
        ids.dtype != torch.int64
        or mask.dtype != torch.int64
        or ids.ndim != 2
        or ids.shape[0] != 1
        or not 1 <= ids.shape[1] <= max_tokens
        or mask.shape != ids.shape
        or (ids < 0).any().item()
        or not ((mask == 0) | (mask == 1)).all().item()
        or not mask.any().item()
    ):
        raise ValueError("Prefix tokens/mask must be one bounded int64 sample without truncation")
    if grid.dtype != torch.int64 or grid.shape != (image_count, 3):
        raise ValueError("Actual image_grid_thw must cover exactly the admitted images")
    patch = processor.image_processor.patch_size
    temporal = processor.image_processor.temporal_patch_size
    if type(patch) is not int or patch <= 0 or type(temporal) is not int or temporal <= 0:
        raise ValueError("Actual processor patch dimensions must be positive integers")
    grids = grid.tolist()
    if any(
        g[0] != 1
        or any(type(v) is not int or v <= 0 for v in g)
        or not MIN_PIXELS <= g[1] * g[2] * patch**2 <= max_pixels
        for g in grids
    ):
        raise ValueError("Actual image grid exceeds the declared per-image pixel budget")
    if (
        pixels.dtype != torch.float32
        or pixels.ndim != 2
        or pixels.shape != (sum(t * h * w for t, h, w in grids), 3 * temporal * patch**2)
        or not torch.isfinite(pixels).all().item()
    ):
        raise ValueError("Actual finite FP32 visual patches must match image_grid_thw")
    return {
        "prefix_tokens": ids.shape[1],
        "actual_image_grid_thw": grids,
        "actual_image_pixels": [h * w * patch**2 for _, h, w in grids],
        "patch_size": patch,
        "temporal_patch_size": temporal,
        "tensors": {key: tensor_record(inputs[key], torch) for key in sorted(FIELDS)},
    }


def process_actor(actor, processor, actor_module):
    messages = actor_module.serialize_navigation_actor_messages(actor)
    images = [
        item["image"]
        for message in messages
        for item in message["content"]
        if item["type"] == "image"
    ]
    try:
        # A new nested mapping AND fresh PIL message images on every call.
        return processor.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
            images_kwargs={"min_pixels": MIN_PIXELS, "max_pixels": MAX_PIXELS},
        )
    finally:
        for image in images:
            image.close()


def verify_sample_pair(
    sample, root, session, processor, *, actor_module, pilot, budget, torch_module
):
    training = actor_module.NavigationActorInput.from_supervision_sample(
        sample, root, budget=budget, session=session
    )
    frames = []
    source_frames = []
    for frame in sample.frames:
        source = pilot.navigation.NavigationSourceFile(frame.path, frame.bytes, frame.sha256)
        raw = source.read(root, maximum_bytes=budget.max_image_bytes)
        frames.append(
            actor_module.NavigationActorFrame.from_observation(
                sequence=frame.step_index,
                image_bytes=raw,
                byte_count=frame.bytes,
                sha256=frame.sha256,
                width=frame.width,
                height=frame.height,
                encoding=frame.encoding,
            )
        )
        source_frames.append(
            {
                "sequence": frame.step_index,
                "path": frame.path,
                "bytes": frame.bytes,
                "sha256": frame.sha256,
                "width": frame.width,
                "height": frame.height,
                "encoding": frame.encoding,
            }
        )
    inference = actor_module.NavigationActorInput.from_observation(
        prompt_original=sample.prompt.text_original,
        current_sequence=sample.step_index,
        frames=tuple(frames),
        budget=budget,
    )
    sequences = [frame.sequence for frame in frames]
    if sequences[-1] != sample.step_index or (
        len(sequences) == 2 and sequences[0] != sample.step_index - 1
    ):
        raise ValueError("Retained RGB must be current plus exactly the latest past observation")
    training_identity, inference_identity = (
        training.to_identity_mapping(),
        inference.to_identity_mapping(),
    )
    if (
        training_identity != inference_identity
        or training.identity_sha256 != inference.identity_sha256
    ):
        raise ValueError("Training/observable actor message identities differ")
    results = []
    for actor in (training, inference):
        inputs = process_actor(actor, processor, actor_module)
        results.append(
            validate_prefix_inputs(
                inputs,
                processor=processor,
                image_count=len(frames),
                torch_module=torch_module,
                tensor_record=pilot.tensor_record,
            )
        )
    if results[0] != results[1]:
        raise ValueError("Training/observable CPU processor prefix tensor bytes differ")
    return {
        "sample_id": sample.sample_id,
        "actor_identity_sha256": training.identity_sha256,
        "source_prompt_sha256": sample.prompt.utf8_sha256,
        "source_frames": source_frames,
        "retained_history_valid": True,
        "actor_identities_equal": True,
        "training_prefix": results[0],
        "observable_prefix": results[1],
        "all_four_tensor_bytes_equal": True,
        "labels_pose_future_ids_entered_model_messages": False,
    }


def official_processor(snapshot, budget):
    import transformers

    if transformers.__version__ != "4.57.6":
        raise ValueError("The original pinned Transformers 4.57.6 processor is required")
    processor = transformers.AutoProcessor.from_pretrained(
        str(snapshot),
        local_files_only=True,
        trust_remote_code=False,
        max_pixels=budget.max_pixels_per_image,
    )
    if type(processor).__name__ != "Qwen3VLProcessor":
        raise ValueError("The pinned local snapshot did not load the official Qwen3VL processor")
    return processor


def capture_sources(
    plan, plan_path, samples, *, pilot, supervisor, processor, actor_module, torch_module
):
    records = {}
    bindings = {}
    root = Path(plan["artifact_root"]).resolve(strict=True)

    def declared(row):
        pilot.binding_shape(row)
        raw = Path(row["path"])
        path = (raw if raw.is_absolute() else root / raw).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("A declared source escaped artifact_root")
        normalized = {"path": str(path), "bytes": row["bytes"], "sha256": row["sha256"]}
        if str(path) in bindings and bindings[str(path)] != normalized:
            raise ValueError("The same source has conflicting byte/SHA declarations")
        bindings[str(path)] = normalized

    for row in pilot.plan_bindings(plan):
        declared(row)
    for sample in samples:
        for value in sample.source.to_mapping().values():
            if isinstance(value, dict) and set(value) == {"path", "bytes", "sha256"}:
                declared(value)
        for frame in sample.frames:
            declared({"path": frame.path, "bytes": frame.bytes, "sha256": frame.sha256})
    # Shared annotation/metadata and the base can be large. Stream each unique
    # declaration once per boundary, never once per retained sample mention.
    for path, row in bindings.items():
        pilot.verify_binding(row, root)
        records[path] = row
    paths = [
        plan_path,
        Path(__file__),
        Path(__file__).with_name("run_onboard_navigation_pilot_supervised.py"),
        ROOT / "tests/test_verify_onboard_navigation_actor_inputs.py",
        Path(actor_module.__file__),
        ROOT / "tests/test_onboard_navigation_actor.py",
        Path(sys.executable),
        Path(torch_module.__file__),
    ]
    if type(processor).__module__ != "builtins":
        for cls in (type(processor), type(processor.image_processor), type(processor.tokenizer)):
            path = inspect.getsourcefile(cls)
            if path:
                paths.append(Path(path))
    for path in paths:
        path = path.resolve(strict=True)
        if str(path) in records:
            continue
        actual = supervisor.file_record(path)
        records[actual["path"]] = actual
    return [records[key] for key in sorted(records)]


def resolve_evaluation_output(output_directory, root, plan, *, supervisor):
    output = supervisor.resolve_fresh(output_directory, root)
    evaluation_root = (root / "artifacts/evaluation").resolve(strict=True)
    if output.parent != evaluation_root or not output.name.startswith(
        "onboard-navigation-actor-inputs-"
    ):
        raise ValueError(
            "Use a new top-level onboard-navigation-actor-inputs-* evaluation directory"
        )
    supervisor.protect_sealed_directories(output, plan, root)
    return output


def verify_actor_inputs(plan_path, plan_sha256, output_directory, *, processor_factory=None):
    if plan_sha256 != PLAN_SHA256:
        raise ValueError("Only the sealed original 86-sample plan is admitted")
    plan_path = Path(plan_path).resolve(strict=True)
    if plan_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Plan exceeds its bounded size")
    raw = plan_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PLAN_SHA256:
        raise ValueError("Plan SHA changed")
    original = json.loads(raw)
    root = Path(original["artifact_root"]).resolve(strict=True)
    if root != ROOT or not plan_path.is_relative_to(root):
        raise ValueError("Original plan must belong to this repository")
    with repository_imports(root):
        supervisor = importlib.import_module("scripts.run_onboard_navigation_pilot_supervised")
        output = resolve_evaluation_output(output_directory, root, original, supervisor=supervisor)
        for row in original["source_files"]:
            path = root / row["path"]
            if supervisor.file_record(path)["sha256"] != row["sha256"]:
                raise ValueError("Frozen implementation SHA changed before import")
        pilot = importlib.import_module("scripts.train_onboard_vlm_navigation_pilot")
        plan = pilot.load_plan(plan_path, PLAN_SHA256)
        pilot.verify_plan_sources(plan)
        samples, _targets = pilot.admit_dataset(plan)
        del _targets  # Supervision is admitted but is never processor input here.
        if len(samples) != 86:
            raise ValueError("All original 86 retained samples must be verified")
        actor_module = importlib.import_module("multidetect.autonomy.onboard_navigation_actor")
        import torch

        if torch.__version__ != "2.6.0+cu124" or torch.cuda.is_initialized():
            raise ValueError("Original Torch runtime and uninitialized CUDA are required")
        budget = pilot.NavigationTrainingBudget()
        budget.validate()
        processor = (processor_factory or official_processor)(
            Path(plan["base"]["snapshot"]), budget
        )
        actor_budget = pilot.navigation.NavigationSupervisionBudget()
        sources_before = capture_sources(
            plan,
            plan_path,
            samples,
            pilot=pilot,
            supervisor=supervisor,
            processor=processor,
            actor_module=actor_module,
            torch_module=torch,
        )
        supervisor.emit(
            "actor_inputs_admitted", sample_count=len(samples), source_count=len(sources_before)
        )
        rows = []
        with pilot.navigation.NavigationSourceValidationSession(
            root, budget=actor_budget
        ) as session:
            session.prime(samples)
            for index, sample in enumerate(samples, 1):
                rows.append(
                    verify_sample_pair(
                        sample,
                        root,
                        session,
                        processor,
                        actor_module=actor_module,
                        pilot=pilot,
                        budget=actor_budget,
                        torch_module=torch,
                    )
                )
                if index % 10 == 0 or index == 86:
                    supervisor.emit("actor_prefix_pair_verified", completed=index, total=86)
        if not session.verified or torch.cuda.is_initialized():
            raise ValueError("Final source session or CPU-only execution check failed")
        sources_after = capture_sources(
            plan,
            plan_path,
            samples,
            pilot=pilot,
            supervisor=supervisor,
            processor=processor,
            actor_module=actor_module,
            torch_module=torch,
        )
        if sources_before != sources_after:
            raise ValueError("Source bindings changed during processor verification")
        report = {
            "schema_version": 1,
            "contract": CONTRACT,
            "native_exit_code": 0,
            "scope": "matched_retained_offline_inputs_not_actual_live_camera_or_sim_real_parity",
            "source_plan_sha256": PLAN_SHA256,
            "actor_contract": actor_module.ACTOR_CONTRACT,
            "system_prompt_sha256": hashlib.sha256(
                actor_module.ACTOR_SYSTEM_PROMPT.encode("utf-8")
            ).hexdigest(),
            "source_bindings_before": sources_before,
            "source_bindings_after": sources_after,
            "source_bindings_unchanged": True,
            "source_validation_session_verified": True,
            "official_processor_used": processor_factory is None,
            "processor": {
                "class": type(processor).__name__,
                "loading_scope": "official_pinned_local_only"
                if processor_factory is None
                else "injected_cpu_test_processor_not_official_acceptance",
                "local_files_only": True if processor_factory is None else None,
                "trust_remote_code": False if processor_factory is None else None,
                "transformers_version": sys.modules["transformers"].__version__
                if processor_factory is None
                else None,
                "torch_version": torch.__version__,
                "snapshot": plan["base"]["snapshot"],
                "min_pixels": MIN_PIXELS,
                "max_pixels": MAX_PIXELS,
                "max_tokens": MAX_TOKENS,
            },
            "sample_count": len(rows),
            "all_86_prefix_pairs_equal": True,
            "max_actual_prefix_tokens": max(
                row["training_prefix"]["prefix_tokens"] for row in rows
            ),
            "max_actual_image_pixels": max(
                value for row in rows for value in row["training_prefix"]["actual_image_pixels"]
            ),
            "samples": rows,
            "cpu_only": True,
            "cuda_initialized": False,
            "model_loaded": False,
            "model_inference_performed": False,
            "optimizer_performed": False,
            "forward_backward_performed": False,
            "training_completed": False,
            "independent_holdout_available": False,
            "quality_claim": False,
            "candidate_promoted": False,
            "physical_performance_claimed": False,
            "hil_real_evidence": False,
            "outdoor_gnss_evidence": False,
            "final_release_allowed": False,
            "execution_authority": False,
            "transport_created": False,
            "mission_created": False,
            "actual_live_camera_parity_verified": False,
            "retained_source_projection": True,
            "independently_captured_live_observations": False,
            "old_training_prefix_identity_claimed": False,
            "sim_real_parity_verified": False,
        }
        output.mkdir(parents=False, exist_ok=False)
        supervisor.create_json(output / "report.json", report)
        supervisor.emit(
            "actor_prefix_verification_completed",
            report=supervisor.file_record(output / "report.json"),
            sample_count=86,
        )
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        verify_actor_inputs(args.plan, args.plan_sha256, args.output_dir)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "actor_prefix_verification_rejected",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            ),
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
