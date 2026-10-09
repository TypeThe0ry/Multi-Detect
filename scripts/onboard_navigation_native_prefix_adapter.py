"""Official processor container to exact four-tensor dict; no tensor conversion.

The frozen r1 live backend deliberately requires built-in dict. Transformers
4.57.6 returns BatchFeature for the native image processor. Admit only that
exact class (or built-in dict), with exact dict data and no extra fields.
"""

from __future__ import annotations

import json

FIELDS = {"input_ids", "attention_mask", "pixel_values", "image_grid_thw"}


def project_native_prefix(raw, *, torch_module, batch_feature_type):
    if type(raw) is dict:
        data = raw
    elif type(raw) is batch_feature_type:
        data = raw.data
    else:
        raise TypeError("Only exact native BatchFeature or built-in dict required")
    if type(data) is not dict or set(data) != FIELDS:
        raise ValueError("Exactly four native prefix fields required; no labels/metadata")
    if any(type(value) is not torch_module.Tensor for value in data.values()):
        raise TypeError("Every native prefix value must be an actual Tensor")
    result = dict(data)
    if any(result[key] is not data[key] for key in FIELDS):
        raise ValueError("Native tensor references changed")
    return result


def canonical_native_prefix_processor(actor, processor):
    # Lazy imports: the exact loader installs storage hooks before HF/PEFT.
    import torch
    from transformers.feature_extraction_utils import BatchFeature

    from scripts.onboard_navigation_inference_backend import _default_prefix_processor

    raw = _default_prefix_processor(actor, processor)
    result = project_native_prefix(raw, torch_module=torch, batch_feature_type=BatchFeature)
    print(
        json.dumps(
            {
                "event": "native_prefix_container_projected",
                "actual_container_type": type(raw).__module__ + "." + type(raw).__qualname__,
                "keys": sorted(result),
                "tensor_references_preserved": True,
                "tensor_conversion_performed": False,
            }
        ),
        flush=True,
    )
    return result
