"""Bounded CPU encoding for the NEW actor, not a model or training launcher.

The caller admits the bound official CPU86 report and the processor/runtime.
Injected processors can exercise these checks but are not official acceptance.
An active source session remains provisional until its successful context exit;
the caller must require ``session.verified`` before using or publishing outputs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def validate_encoding_budget(budget):
    """Only encoding limits apply here; never invoke the old 100-step validator."""
    for name, expected in (
        ("max_pixels_per_image", 50176),
        ("max_input_tokens", 4096),
        ("max_new_tokens", 192),
    ):
        value = getattr(budget, name, None)
        if type(value) is not int or value != expected:
            raise ValueError("NEW encoding requires the declared 50176/4096/192 limits")


def _process_full(actor, target, processor, actor_module, checker):
    messages = actor_module.serialize_navigation_actor_messages(actor)
    images = [
        item["image"]
        for message in messages
        for item in message["content"]
        if item["type"] == "image"
    ]
    try:
        messages.append({"role": "assistant", "content": [{"type": "text", "text": target}]})
        return processor.apply_chat_template(
            messages,
            add_generation_prompt=False,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
            images_kwargs={"min_pixels": checker.MIN_PIXELS, "max_pixels": checker.MAX_PIXELS},
        )
    finally:
        # Save references before processing: processors may consume the messages.
        for image in images:
            image.close()


def encode_navigation_sample(
    sample,
    artifact_root,
    *,
    processor,
    torch_module,
    budget,
    prefix_reference,
    session,
):
    """Encode a frozen-actor inference prefix and response-only training full chat.

    ``prefix_reference`` is the complete per-sample row, not merely its tensor
    section. Its containing CPU86 report must already have passed the caller's
    official/report SHA admission. ``prefix_reference_verified`` describes only
    exact row equality; it does not claim the injected processor is official.
    """
    validate_encoding_budget(budget)
    if type(prefix_reference) is not dict:
        raise ValueError("A complete bound CPU86 prefix reference row is required")

    # Optional image/tensor dependencies remain lazy and supplied by the caller.
    from multidetect.autonomy import onboard_navigation_actor as actor_module
    from multidetect.autonomy import onboard_navigation_supervision as navigation
    from scripts import train_onboard_vlm_navigation_pilot as pilot
    from scripts import verify_onboard_navigation_actor_inputs as checker

    torch, core = torch_module, pilot.core
    source_budget = navigation.NavigationSupervisionBudget()
    training_actor = actor_module.NavigationActorInput.from_supervision_sample(
        sample, Path(artifact_root), budget=source_budget, session=session
    )
    inference_actor = actor_module.NavigationActorInput.from_observation(
        prompt_original=training_actor.prompt_original,
        current_sequence=training_actor.current_sequence,
        frames=training_actor.frames,
        budget=source_budget,
    )
    sequences = [frame.sequence for frame in training_actor.frames]
    if sequences[-1] != sample.step_index or (
        len(sequences) == 2 and sequences[0] != sample.step_index - 1
    ):
        raise ValueError("Retained RGB must be current plus exactly the latest past observation")
    identity = training_actor.identity_sha256
    if (
        training_actor.to_identity_mapping() != inference_actor.to_identity_mapping()
        or identity != inference_actor.identity_sha256
    ):
        raise ValueError("Training/inference frozen actor identities differ")

    def admit(inputs):
        return checker.validate_prefix_inputs(
            inputs,
            processor=processor,
            image_count=len(training_actor.frames),
            torch_module=torch,
            tensor_record=pilot.tensor_record,
            max_pixels=budget.max_pixels_per_image,
            max_tokens=budget.max_input_tokens,
        )

    prefix = checker.process_actor(inference_actor, processor, actor_module)
    prefix_info = admit(prefix)
    # Own the admitted bytes even if a processor later reuses its output buffers.
    prefix = {key: tensor.clone() for key, tensor in prefix.items()}
    target = navigation.serialize_navigation_supervision_target(
        sample, Path(artifact_root), budget=source_budget, session=session
    )
    full = _process_full(training_actor, target, processor, actor_module, checker)
    full_info = admit(full)
    full = {key: tensor.clone() for key, tensor in full.items()}
    prefix_tokens, total_tokens = prefix_info["prefix_tokens"], full_info["prefix_tokens"]
    response_tokens = total_tokens - prefix_tokens
    if not 1 <= response_tokens <= budget.max_new_tokens:
        raise ValueError("Actual assistant suffix exceeds the declared 192-token cap or is empty")

    full_prefix = {
        key: tensor[:, :prefix_tokens] if key in {"input_ids", "attention_mask"} else tensor
        for key, tensor in full.items()
    }
    # Records compare raw bytes, including signed zero, not merely float values.
    if core.canonical_bytes(admit(full_prefix)) != core.canonical_bytes(prefix_info):
        raise ValueError("Full chat does not preserve exact token/mask/grid/pixel prefix bytes")

    row = {
        "sample_id": sample.sample_id,
        "actor_identity_sha256": identity,
        "source_prompt_sha256": sample.prompt.utf8_sha256,
        "source_frames": [
            {
                "sequence": frame.step_index,
                "path": frame.path,
                "bytes": frame.bytes,
                "sha256": frame.sha256,
                "width": frame.width,
                "height": frame.height,
                "encoding": frame.encoding,
            }
            for frame in sample.frames
        ],
        "retained_history_valid": True,
        "actor_identities_equal": True,
        "training_prefix": prefix_info,
        "observable_prefix": prefix_info,
        "all_four_tensor_bytes_equal": True,
        "labels_pose_future_ids_entered_model_messages": False,
    }
    # Canonical JSON distinguishes bool/int values and rejects non-finite data.
    if core.canonical_bytes(prefix_reference) != core.canonical_bytes(row):
        raise ValueError("Complete CPU86 prefix reference row differs from NEW encoding")

    image_tokens = {
        processor.tokenizer.convert_tokens_to_ids(value)
        for value in ("<|image_pad|>", "<|video_pad|>", "<|vision_start|>", "<|vision_end|>")
    }
    if any(type(value) is not int or value < 0 for value in image_tokens):
        raise ValueError("Actual tokenizer must identify all four visual special tokens")
    full_ids, prefix_ids = full["input_ids"][0].tolist(), prefix["input_ids"][0].tolist()
    mask = full["attention_mask"][0].tolist()
    labels = core.response_only_labels(full_ids, prefix_ids, mask, image_tokens)
    if any(
        label != -100
        for index, label in enumerate(labels)
        if index < prefix_tokens or mask[index] == 0 or full_ids[index] in image_tokens
    ):
        raise ValueError("Prompt, image and padding tokens must never be supervised")
    return {
        "prefix": prefix,
        "full": full,
        "labels": torch.tensor([labels], dtype=torch.int64, device="cpu"),
        "prefix_tokens": prefix_tokens,
        "total_tokens": total_tokens,
        "response_tokens": response_tokens,
        "supervised_tokens": sum(label != -100 for label in labels),
        "prefix_tensors": prefix_info["tensors"],
        "input_tensors": full_info["tensors"],
        "response_labels_sha256": hashlib.sha256(core.canonical_bytes(labels)).hexdigest(),
        "target_sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
        "actor_identity_sha256": identity,
        "prefix_reference_verified": True,
        "prompt_label_mask_verified": True,
    }
