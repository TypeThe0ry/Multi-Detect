from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from multidetect.appearance_reid import (
    OnnxPersonReIdConfig,
    OnnxPersonReIdEncoder,
    ReIdModelContractError,
)
from multidetect.domain import BoundingBox, Detection


class _Session:
    def __init__(
        self,
        *,
        input_shape=("batch", 3, 256, 128),
        output_shape=("batch", 256),
        output=None,
    ) -> None:
        self.input_shape = input_shape
        self.output_shape = output_shape
        self.output = output
        self.last_feed = None

    def get_inputs(self):
        return (SimpleNamespace(name="input", shape=self.input_shape),)

    def get_outputs(self):
        return (SimpleNamespace(name="fc_pred", shape=self.output_shape),)

    def get_providers(self):
        return ("CPUExecutionProvider",)

    def run(self, outputs, feeds):
        assert outputs == ["fc_pred"]
        self.last_feed = feeds["input"]
        if self.output is not None:
            return [self.output]
        batch = self.last_feed.shape[0]
        result = np.zeros((batch, 256), dtype=np.float32)
        for index in range(batch):
            result[index, index] = 2.0
            result[index, index + 8] = 1.0
        return [result]


def _config(tmp_path) -> OnnxPersonReIdConfig:
    return OnnxPersonReIdConfig(model_path=tmp_path / "unused.onnx")


def test_person_reid_batches_only_validated_person_classes_and_normalizes_embeddings(
    tmp_path,
) -> None:
    session = _Session()
    encoder = OnnxPersonReIdEncoder(_config(tmp_path), session=session)
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    image[100:350, 100:250] = (20, 80, 200)
    detections = (
        Detection("person", 0.95, BoundingBox(0.1, 0.1, 0.4, 0.8)),
        Detection("vehicle", 0.90, BoundingBox(0.5, 0.4, 0.9, 0.8)),
        Detection("firefighter", 0.91, BoundingBox(0.2, 0.2, 0.5, 0.9)),
    )

    observations = encoder.encode_detections(image, detections)

    assert session.last_feed.shape == (2, 3, 256, 128)
    assert observations[0].appearance is not None
    assert observations[1].appearance is None
    assert observations[1].appearance_reliable is False
    assert observations[2].appearance is not None
    assert sum(value * value for value in observations[0].appearance.values) == pytest.approx(1.0)
    assert encoder.provider_names == ("CPUExecutionProvider",)


def test_person_reid_preprocessing_is_rgb_normalized_nchw(tmp_path) -> None:
    session = _Session()
    encoder = OnnxPersonReIdEncoder(_config(tmp_path), session=session)
    image = np.zeros((40, 20, 3), dtype=np.uint8)
    image[:, :] = (0, 0, 255)

    encoder.encode_detections(
        image,
        (Detection("person", 0.9, BoundingBox(0.1, 0.1, 0.9, 0.9)),),
    )

    tensor = session.last_feed
    assert tensor.shape == (1, 3, 256, 128)
    assert tensor.dtype == np.float32
    assert float(tensor[0, 0].mean()) == pytest.approx((1.0 - 0.485) / 0.226, rel=1e-4)
    assert float(tensor[0, 2].mean()) == pytest.approx((0.0 - 0.406) / 0.226, rel=1e-4)


@pytest.mark.parametrize(
    ("input_shape", "output_shape", "message"),
    [
        (("batch", 3, 224, 224), ("batch", 256), "input shape"),
        (("batch", 3, 256, 128), ("batch", 128), "feature size"),
    ],
)
def test_person_reid_rejects_model_shape_contract_mismatch(
    tmp_path,
    input_shape,
    output_shape,
    message,
) -> None:
    with pytest.raises(ReIdModelContractError, match=message):
        OnnxPersonReIdEncoder(
            _config(tmp_path),
            session=_Session(input_shape=input_shape, output_shape=output_shape),
        )


def test_person_reid_rejects_wrong_runtime_output_shape(tmp_path) -> None:
    session = _Session(output=np.zeros((1, 128), dtype=np.float32))
    encoder = OnnxPersonReIdEncoder(_config(tmp_path), session=session)

    with pytest.raises(ReIdModelContractError, match="output shape"):
        encoder.encode_detections(
            np.zeros((32, 32, 3), dtype=np.uint8),
            (Detection("person", 0.9, BoundingBox(0.1, 0.1, 0.9, 0.9)),),
        )


def test_person_reid_rejects_unpinned_artifact_before_loading_runtime(tmp_path) -> None:
    artifact = tmp_path / "tampered.onnx"
    artifact.write_bytes(b"not-the-pinned-model")

    with pytest.raises(ReIdModelContractError, match="SHA-256"):
        OnnxPersonReIdEncoder(OnnxPersonReIdConfig(model_path=artifact))
