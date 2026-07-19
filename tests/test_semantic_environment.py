from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from multidetect.semantic_environment import (
    CITYSEMSEGFORMER_LABELS,
    AsyncSemanticContextRunner,
    CategoricalSemanticMaskAdapter,
    OnnxCategoricalSemanticContext,
    OnnxSemanticContextConfig,
    SemanticContextState,
    SemanticMaskConfig,
)

ROOT = Path(__file__).resolve().parents[1]


def test_city_semsegformer_labels_match_locked_official_file() -> None:
    labels = tuple(
        line.strip()
        for line in (ROOT / "configs/models/citysemsegformer/labels.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    )
    assert labels == CITYSEMSEGFORMER_LABELS


def test_semantic_adapter_extracts_building_and_road_without_fake_confidence() -> None:
    mask = np.full((1, 100, 120, 1), 10, dtype=np.int32)  # sky
    mask[:, 60:100, :, :] = 0  # road
    mask[:, 15:55, 10:45, :] = 2  # building one
    mask[:, 20:50, 70:105, :] = 2  # building two

    regions = CategoricalSemanticMaskAdapter().extract(mask)

    assert [region.label for region in regions] == ["road", "building", "building"]
    assert all(not hasattr(region, "confidence") for region in regions)
    assert all(region.categorical_mask_only for region in regions)
    assert all(region.advisory_only for region in regions)
    assert all(region.flight_control_enabled is False for region in regions)
    assert all(region.physical_release_enabled is False for region in regions)
    assert regions[0].bbox.y1 == pytest.approx(0.60)


def test_semantic_adapter_ignores_small_components_and_void() -> None:
    mask = np.full((40, 40), 19, dtype=np.int32)
    mask[5:8, 5:8] = 2
    adapter = CategoricalSemanticMaskAdapter(SemanticMaskConfig(minimum_component_pixels=16))
    assert adapter.extract(mask) == ()


@pytest.mark.parametrize(
    "output,message",
    [
        (np.zeros((2, 3, 4, 1)), "shape"),
        (np.array([[0.5]], dtype=np.float32), "non-integer"),
        (np.array([[99]], dtype=np.int32), "unknown class IDs"),
        (np.array([[np.nan]], dtype=np.float32), "non-finite"),
    ],
)
def test_semantic_adapter_rejects_malformed_masks(output, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CategoricalSemanticMaskAdapter().extract(output)


def test_environment_model_source_lock_preserves_unfinished_gates() -> None:
    document = json.loads(
        (ROOT / "configs/models/environment_model_source_lock.json").read_text(encoding="utf-8")
    )
    by_role = {item["role"]: item for item in document["sources"]}
    assert by_role["building_road_semantic_context"]["status"] == (
        "official_onnx_downloaded_hash_verified_local_not_deployed"
    )
    assert by_role["building_road_semantic_context"]["artifact_sha256"] == (
        "94ace62e250ed0a3122a46df8573950510b60a90c1b511e53c40dbca2bea21fb"
    )
    assert by_role["power_line_thin_structure_segmentation"]["source_commit"] == (
        "72ddf48cfee6d25b89fa8063e4dcd44bad08cddb"
    )
    assert by_role["storage_tank_aerial_obb"]["dataset_license"] == (
        "review_required_before_download_or_training"
    )
    for item in by_role["building_road_semantic_context"]["metadata_files"]:
        artifact = ROOT / item["path"]
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == item["sha256"]
    assert document["flight_control_enabled"] is False
    assert document["physical_release_enabled"] is False


def test_semantic_context_contract_is_bounded_categorical_and_unfinished() -> None:
    document = json.loads(
        (ROOT / "configs/models/semantic_context_contract.json").read_text(encoding="utf-8")
    )
    runtime = document["runtime_contract"]
    assert document["status"] == "official_onnx_downloaded_hash_verified_local_not_deployed"
    assert document["required_labels"] == ["road", "building"]
    assert runtime["output_format"] == "categorical_H_W_1"
    assert runtime["confidence_available"] is False
    assert runtime["queue_capacity"] == 1
    assert document["identity_policy"]["target_pool_identity_authority"] is False
    assert document["production_approved"] is False
    assert document["flight_control_enabled"] is False
    assert document["physical_release_enabled"] is False


class _SemanticSession:
    def __init__(self, *, input_shape=(1, 3, 1024, 1820), output_shape=(1, 1024, 1820, 1)):
        self.input_shape = input_shape
        self.output_shape = output_shape
        self.last_tensor = None
        self.close_count = 0

    def get_inputs(self):
        return (SimpleNamespace(name="input", shape=self.input_shape),)

    def get_outputs(self):
        return (SimpleNamespace(name="output", shape=self.output_shape),)

    def get_providers(self):
        return ("TestExecutionProvider",)

    def run(self, output_names, feed):
        assert output_names == ("output",)
        self.last_tensor = feed["input"]
        mask = np.full(self.output_shape, 10, dtype=np.int32)
        mask[:, 700:, :, :] = 0
        mask[:, 200:650, 200:800, :] = 2
        return (mask,)

    def close(self) -> None:
        self.close_count += 1


def test_semantic_onnx_wrapper_applies_official_preprocessing_and_mask_adapter() -> None:
    session = _SemanticSession()
    model = OnnxCategoricalSemanticContext(
        OnnxSemanticContextConfig(Path("citysemsegformer.onnx")),
        session=session,
    )
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    image[:, :, 0] = 10
    image[:, :, 1] = 20
    image[:, :, 2] = 30

    regions = model.infer(image)

    assert model.provider_names == ("TestExecutionProvider",)
    assert session.last_tensor.shape == (1, 3, 1024, 1820)
    expected_red = (30.0 - 123.675) * 0.01735207357279195
    assert float(session.last_tensor[0, 0, 0, 0]) == pytest.approx(expected_red)
    assert {region.label for region in regions} == {"road", "building"}
    assert all(not hasattr(region, "confidence") for region in regions)
    model.close()
    assert session.close_count == 1


@pytest.mark.parametrize(
    "input_shape,output_shape,message",
    [
        ((1, 3, 512, 512), (1, 1024, 1820, 1), "input shape"),
        ((1, 3, 1024, 1820), (1, 20, 1024, 1820), "output shape"),
    ],
)
def test_semantic_onnx_wrapper_rejects_shape_drift(input_shape, output_shape, message) -> None:
    with pytest.raises(ValueError, match=message):
        OnnxCategoricalSemanticContext(
            OnnxSemanticContextConfig(Path("citysemsegformer.onnx")),
            session=_SemanticSession(input_shape=input_shape, output_shape=output_shape),
        )


class _BlockingSemanticModel:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.seen_markers: list[int] = []

    def infer(self, image_bgr):
        self.seen_markers.append(int(image_bgr[0, 0, 0]))
        self.started.set()
        assert self.release.wait(2.0)
        return ()


def test_async_semantic_context_keeps_only_latest_pending_frame() -> None:
    model = _BlockingSemanticModel()
    runner = AsyncSemanticContextRunner(model, minimum_interval_s=0.001)
    runner.start()
    base_s = time.monotonic()
    try:
        assert runner.submit(
            np.full((8, 8, 3), 1, dtype=np.uint8),
            frame_id="frame-1",
            captured_at_s=base_s,
            submitted_at_s=base_s,
        )
        assert model.started.wait(1.0)
        assert runner.submit(
            np.full((8, 8, 3), 2, dtype=np.uint8),
            frame_id="frame-2",
            captured_at_s=base_s,
            submitted_at_s=base_s + 0.01,
        )
        assert runner.submit(
            np.full((8, 8, 3), 3, dtype=np.uint8),
            frame_id="frame-3",
            captured_at_s=base_s,
            submitted_at_s=base_s + 0.02,
        )
        model.release.set()
        snapshot = runner.wait_for_snapshot(frame_id="frame-3", timeout_s=2.0)
        assert snapshot is not None
        assert snapshot.frame_id == "frame-3"
        assert snapshot.state is SemanticContextState.VALID
        assert model.seen_markers == [1, 3]
        statistics = runner.statistics()
        assert statistics.submitted_frame_count == 3
        assert statistics.replaced_pending_frame_count == 1
        assert statistics.completed_frame_count == 2
        assert statistics.pending_frame_count == 0
        assert statistics.queue_capacity == 1
    finally:
        model.release.set()
        assert runner.close()


def test_async_semantic_context_rate_limits_without_copying_backlog() -> None:
    model = _BlockingSemanticModel()
    model.release.set()
    runner = AsyncSemanticContextRunner(model, minimum_interval_s=1.0)
    runner.start()
    base_s = time.monotonic()
    try:
        image = np.zeros((4, 4, 3), dtype=np.uint8)
        assert runner.submit(
            image,
            frame_id="frame-1",
            captured_at_s=base_s,
            submitted_at_s=base_s,
        )
        assert not runner.submit(
            image,
            frame_id="frame-2",
            captured_at_s=base_s,
            submitted_at_s=base_s + 0.1,
        )
        assert runner.wait_for_snapshot(frame_id="frame-1", timeout_s=1.0) is not None
        statistics = runner.statistics()
        assert statistics.submitted_frame_count == 1
        assert statistics.interval_skipped_frame_count == 1
        assert statistics.replaced_pending_frame_count == 0
    finally:
        assert runner.close()


class _FailingSemanticModel:
    def infer(self, _image_bgr):
        raise RuntimeError("sensitive backend details must not leave the worker")


def test_async_semantic_context_sanitizes_failure_and_never_enables_control() -> None:
    runner = AsyncSemanticContextRunner(_FailingSemanticModel(), minimum_interval_s=0.001)
    runner.start()
    now_s = time.monotonic()
    try:
        runner.submit(
            np.zeros((4, 4, 3), dtype=np.uint8),
            frame_id="frame-7",
            captured_at_s=now_s,
            submitted_at_s=now_s,
        )
        snapshot = runner.wait_for_snapshot(frame_id="frame-7", timeout_s=1.0)
        assert snapshot is not None
        assert snapshot.state is SemanticContextState.INVALID
        assert snapshot.regions == ()
        assert snapshot.error_type == "RuntimeError"
        assert snapshot.advisory_only is True
        assert snapshot.flight_control_enabled is False
        assert snapshot.physical_release_enabled is False
        assert runner.statistics().failed_frame_count == 1
    finally:
        assert runner.close()


class _ClosableSemanticModel:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def test_async_semantic_context_closes_unstarted_model_resource() -> None:
    model = _ClosableSemanticModel()
    runner = AsyncSemanticContextRunner(model)

    assert runner.close()
    assert model.close_count == 1
