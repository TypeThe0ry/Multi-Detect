from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from multidetect.appearance_reid import ReIdModelContractError
from multidetect.domain import BoundingBox, Detection
from multidetect.vehicle_reid import OnnxVehicleReIdConfig, OnnxVehicleReIdEncoder


class _Session:
    def __init__(
        self,
        *,
        input_shape=("batch_size", "channels", "height", "width"),
        output_shape=("batch_size", 512),
        output=None,
    ) -> None:
        self.input_shape = input_shape
        self.output_shape = output_shape
        self.output = output
        self.feeds = []

    def get_inputs(self):
        return (SimpleNamespace(name="input", shape=self.input_shape),)

    def get_outputs(self):
        return (SimpleNamespace(name="output", shape=self.output_shape),)

    def get_providers(self):
        return ("CPUExecutionProvider",)

    def run(self, outputs, feeds):
        assert outputs == ["output"]
        tensor = feeds["input"]
        self.feeds.append(tensor)
        if self.output is not None:
            return [self.output]
        result = np.zeros((tensor.shape[0], 512), dtype=np.float32)
        result[:, 2] = 2.0
        result[:, 20] = 1.0
        return [result]


def _config(tmp_path, **values) -> OnnxVehicleReIdConfig:
    return OnnxVehicleReIdConfig(model_path=tmp_path / "unused.onnx", **values)


def test_vehicle_reid_encodes_only_validated_car_body_labels(tmp_path) -> None:
    session = _Session()
    encoder = OnnxVehicleReIdEncoder(_config(tmp_path), session=session)
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    detections = (
        Detection("car", 0.95, BoundingBox(0.1, 0.2, 0.4, 0.7)),
        Detection("person", 0.90, BoundingBox(0.5, 0.1, 0.7, 0.8)),
        Detection("truck", 0.91, BoundingBox(0.4, 0.4, 0.9, 0.8)),
        Detection("van", 0.89, BoundingBox(0.1, 0.1, 0.35, 0.55)),
        Detection("motorcycle", 0.88, BoundingBox(0.2, 0.2, 0.4, 0.5)),
    )

    observations = encoder.encode_detections(image, detections)

    assert len(session.feeds) == 3
    assert all(feed.shape == (1, 3, 208, 208) for feed in session.feeds)
    assert observations[0].appearance is not None
    assert observations[1].appearance is None
    assert observations[2].appearance is not None
    assert observations[3].appearance is not None
    assert observations[4].appearance is None
    assert observations[1].appearance_reliable is False
    assert observations[4].appearance_reliable is False
    assert sum(value * value for value in observations[0].appearance.values) == pytest.approx(1.0)
    assert encoder.provider_names == ("CPUExecutionProvider",)


def test_vehicle_reid_preprocessing_is_raw_rgb_nchw(tmp_path) -> None:
    session = _Session()
    encoder = OnnxVehicleReIdEncoder(_config(tmp_path), session=session)
    image = np.zeros((40, 40, 3), dtype=np.uint8)
    image[:, :] = (10, 20, 230)

    encoder.encode_detections(
        image,
        (Detection("car", 0.9, BoundingBox(0.1, 0.1, 0.9, 0.9)),),
    )

    tensor = session.feeds[0]
    assert tensor.dtype == np.float32
    assert float(tensor[0, 0].mean()) == pytest.approx(230.0)
    assert float(tensor[0, 1].mean()) == pytest.approx(20.0)
    assert float(tensor[0, 2].mean()) == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("input_shape", "output_shape", "message"),
    [
        (("batch", 3, 224, 224), ("batch", 512), "input shape"),
        (("batch", 3, 208, 208), ("batch", 256), "feature size"),
    ],
)
def test_vehicle_reid_rejects_shape_contract_mismatch(
    tmp_path,
    input_shape,
    output_shape,
    message,
) -> None:
    with pytest.raises(ReIdModelContractError, match=message):
        OnnxVehicleReIdEncoder(
            _config(tmp_path),
            session=_Session(input_shape=input_shape, output_shape=output_shape),
        )


def test_vehicle_reid_rejects_unpinned_artifact_before_loading_runtime(tmp_path) -> None:
    artifact = tmp_path / "tampered.onnx"
    artifact.write_bytes(b"not-the-pinned-model")

    with pytest.raises(ReIdModelContractError, match="SHA-384"):
        OnnxVehicleReIdEncoder(OnnxVehicleReIdConfig(model_path=artifact))
