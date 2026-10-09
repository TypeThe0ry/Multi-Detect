"""Real BatchFeature/tiny CPU tensors; not a model or Gazebo fixture."""

from collections import UserDict

import pytest
import torch
from transformers.feature_extraction_utils import BatchFeature

from scripts.onboard_navigation_native_prefix_adapter import FIELDS, project_native_prefix


def tensors():
    return {key: torch.tensor([1], dtype=torch.int64) for key in FIELDS}


@pytest.mark.parametrize("native_type", [dict, BatchFeature])
def test_native_four_tensor_shallow_projection_preserves_every_object(native_type):
    original = tensors()
    raw = native_type(original)
    result = project_native_prefix(raw, torch_module=torch, batch_feature_type=BatchFeature)
    assert type(result) is dict
    assert set(result) == FIELDS
    assert all(result[key] is original[key] for key in FIELDS)
    assert all(value.device.type == "cpu" for value in result.values())


@pytest.mark.parametrize("extra", ["labels", "metadata", "past_key_values"])
def test_extra_fields_not_silently_filtered(extra):
    raw = BatchFeature({**tensors(), extra: torch.tensor([1])})
    with pytest.raises(ValueError, match="Exactly four"):
        project_native_prefix(raw, torch_module=torch, batch_feature_type=BatchFeature)


def test_missing_field_rejected():
    raw = BatchFeature(tensors())
    del raw.data["pixel_values"]
    with pytest.raises(ValueError, match="Exactly four"):
        project_native_prefix(raw, torch_module=torch, batch_feature_type=BatchFeature)


@pytest.mark.parametrize("value", [None, [1], 1, "tensor"])
def test_non_tensor_never_coerced(value):
    raw = BatchFeature({**tensors(), "pixel_values": value})
    with pytest.raises(TypeError, match="Tensor"):
        project_native_prefix(raw, torch_module=torch, batch_feature_type=BatchFeature)


def test_unrelated_mapping_and_subclass_rejected():
    class Untrusted(BatchFeature):
        pass

    for raw in (UserDict(tensors()), Untrusted(tensors())):
        with pytest.raises(TypeError, match="exact native"):
            project_native_prefix(raw, torch_module=torch, batch_feature_type=BatchFeature)


def test_batchfeature_non_dict_data_rejected():
    raw = BatchFeature(tensors())
    raw.data = UserDict(raw.data)
    with pytest.raises(ValueError, match="Exactly four"):
        project_native_prefix(raw, torch_module=torch, batch_feature_type=BatchFeature)
